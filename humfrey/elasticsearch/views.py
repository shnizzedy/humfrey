

import calendar
import copy
import math
import time
import re
import urllib.request, urllib.parse, urllib.error

from django.http import HttpResponse
from django_conneg.decorators import renderer
from django_conneg.http import HttpResponseSeeOther
from django_conneg.views import HTMLView, JSONPView
import rdflib

from humfrey.sparql.views import StoreView
from humfrey.sparql.utils import get_labels
from humfrey.linkeddata.uri import doc_forwards
from humfrey.linkeddata.views import MappingView
from humfrey.utils.namespaces import expand, contract
from humfrey.utils import json

from .forms import SearchForm
from .query import ElasticSearchEndpoint
from .opensearch import OpenSearchView

class ElasticSearchView(HTMLView, JSONPView, MappingView, StoreView):
    def get(self, request):
        query = self.get_query()
        if query is not None:
            results = self.get_results(query)
            self.context.update(results)
        self.finalize_context()
        return self.render()

    @property
    def search_endpoint(self):
        try:
            return self._search_endpoint
        except AttributeError:
            type_name = self.request.GET['type'] if re.match(r'^[a-z\-\d]+$', self.request.GET.get('type', '')) else None
            self._search_endpoint = ElasticSearchEndpoint(self.store, type_name)
            return self._search_endpoint

    def get_query(self):
        return None
    
    def get_results(self, query):
        results = self.search_endpoint.query(query)

        results['q'] = query

        for hit in results['hits']['hits']:
            try:
                hit['_url'] = doc_forwards(hit['_source']['uri'])[None]
            except KeyError:
                raise

        return results

    @classmethod
    def strip_underscores(cls, value):
        if isinstance(value, dict):
            for key, subvalue in list(value.items()):
                if key.startswith('_'):
                    value[key[1:]] = value.pop(key)
                cls.strip_underscores(subvalue)
        if isinstance(value, list):
            for subvalue in value:
                cls.strip_underscores(subvalue)

    def finalize_context(self):
        pass

    @renderer(format="html", mimetypes=('text/html', 'application/xhtml+xml'), priority=1, name='HTML')
    def render_html(self, request, context, template_name):
        if 'hits' in context:
            self.strip_underscores(context['hits'])
        return super(ElasticSearchView, self).render_html(request, context, template_name)

    @renderer(format="autocomplete", name="Autocomplete JSON")
    def render_autocomplete(self, request, context, template_name):
        if not context.get('hits'):
            raise self.MissingQuery()
        context = [{'value': hit['_source']['uri'],
                    'altNames': '\t'.join(l for l in hit['_source'].get('altLabel', []) + hit['_source'].get('hiddenLabel', [])),
                    'label': hit['_source']['label']} for hit in context['hits']['hits']]
        content, mimetype = json.dumps(context), 'application/json'
        if 'callback' in request.GET:
            content, mimetype = [request.GET['callback'], '(', content, ');'], 'application/javascript'
        return HttpResponse(content, mimetype=mimetype)

    def error(self, request, exception, args, kwargs, status_code):
        if isinstance(exception, self.MissingQuery):
            return self.error_view(request,
                                   {'error': {'status_code': 400,
                                              'message': "Missing 'q' parameter."}},
                                   'elasticsearch/bad_request')
        else:
            return super(SearchView, self).error(request, exception, args, kwargs, status_code)


class SearchView(HTMLView, JSONPView, MappingView, OpenSearchView, StoreView):
    page_size = 10

    # e.g. {'filter.category.uri': ('filter.subcategory.uri',)}
    dependent_parameters = {}

    aggregations = {'type': {'terms': {'field': 'type.uri',
                                          'size': 20}}}
    template_name = 'elasticsearch/search'
    default_search_item_template_name = 'elasticsearch/search_item'
    default_types = None
    
    class MissingQuery(Exception):
        pass

    @classmethod
    def strip_underscores(cls, value):
        if isinstance(value, dict):
            for key, subvalue in list(value.items()):
                if key.startswith('_'):
                    value[key[1:]] = value.pop(key)
                cls.strip_underscores(subvalue)
        if isinstance(value, list):
            for subvalue in value:
                cls.strip_underscores(subvalue)

    @property
    def search_endpoint(self):
        try:
            return self._search_endpoint
        except AttributeError:
            type_name = self.request.GET['type'] if re.match(r'^[a-z\-\d]+$', self.request.GET.get('type', '')) else None
            self._search_endpoint = ElasticSearchEndpoint(self.store, type_name)
            return self._search_endpoint

    def get(self, request):
        context = self.context
        form = SearchForm(request.GET or None)
        context.update({'form': form,
                        'base_url': request.build_absolute_uri(),
                        'dependent_parameters': self.dependent_parameters,
                        'default_search_item_template_name': self.default_search_item_template_name})
        
        if form.is_valid():
            self.context.update(self.get_results(dict((k, request.GET[k]) for k in request.GET),
                                            form.cleaned_data))
            if context['page'] > context['page_count']:
                query = copy.copy(request.GET)
                query['page'] = context['page_count']
                query = urllib.parse.urlencode(query)
                return HttpResponseSeeOther('{path}?{query}'.format(path=request.path,
                                                                    query=query))

        context = self.finalize_context(request, context)

        return self.render()

    def finalize_context(self, request, context):
        return context

    def get_query(self, parameters, cleaned_data, start, page_size):
        
        default_operator = parameters.get('default_operator', '').upper()
        if default_operator not in ('AND', 'OR'):
            default_operator = 'AND'

        query = {
            'query': {
                'bool': {
                    'must': {
                        'query_string': {'query': cleaned_data['q'],
                                         'default_operator': default_operator}},
                    'filter':[],
                },
            },
            'from': start,
            'size': page_size,
        }

        # Parse query parameters of the form 'FTYPE.FIELDNAME'.
        filter_fields = set()
        for key, values in self.request.GET.lists():
            if '.' not in key:
                continue
            ftype, field = key.split('.', 1)
            filters = []
            for value in values:
                if not value:
                    continue

                if ftype == 'filter':
                    if value == '-':
                        filter = {'missing': {'field': field}}
                    else:
                        if field.endswith('.uri') and ':' in value:
                            value = expand(value)
                        filter = {'term': {field: value}}
                elif ftype == 'not':
                    if field.endswith('.uri') and ':' in value:
                        value = expand(value)
                    filter = {'not': {'term': {field: value}}}
                elif ftype in ('gt', 'gte', 'lt', 'lte'):
                    if value == 'now':
                        value = int(calendar.timegm(time.gmtime()) * 1000)
                    filter = {'range': {field : {ftype: value}}}
                else:
                    continue
                filters.append(filter)
            if len(filters) == 1:
                query['query']['bool']['filter'].append(filters[0])
            elif len(filters) > 1:
                query['query']['bool']['filter'].append({'or': filters})
            else:
                continue
            filter_fields.add(field)

        if self.aggregations:
            # Copy the aggregation definitions as we'll be playing with them shortly.
            aggregations = copy.deepcopy(self.aggregations)

            # Add aggregation filters for all active filters except any acting on this
            # particular aggregation.
            if 'filter' in query:
                for aggregation in aggregations.values():
                    for filter in query['filter']:
                        if aggregation['terms']['field'] not in filter_fields:
                            if 'facet_filter' not in aggregation:
                                aggregation['facet_filter'] = []
                            aggregation['facet_filter'].append(filter)
            query['aggregations'] = aggregations

        # If default_types set, add a filter to restrict the results.
        if self.default_types and 'type' not in self.request.GET:
            query['filter'].append({'or': [{'type': {'value': t}} for t in self.default_types]})

        if not query['query']['bool']['filter']:
            del query['query']['bool']['filter']

        return query

    def get_results(self, parameters, cleaned_data):
        page = cleaned_data.get('page') or 1
        page_size = cleaned_data.get('page_size') or self.page_size
        start = (page - 1) * page_size

        query = self.get_query(parameters, cleaned_data, start, page_size)

        # If there aren't any filters defined, we don't want a filter part of
        # our query.
        if 'bool' in query['query'] and 'filter' in query['query']['bool']:
            if not query['query']['bool']['filter']:
                del query['query']['bool']['filter']

        results = self.search_endpoint.query(query)

        results.update(self.get_pagination(page_size, page, start, results))
        results['q'] = cleaned_data['q']

        aggregation_labels = set()
        for key in query['aggregations']:
            meta = results['aggregations'][key]['meta'] = query['aggregations'][key]
            filter_value = parameters.get('filter.%s' % query['aggregations'][key]['terms']['field'])
            results['aggregations'][key]['filter'] = {'present': filter_value is not None,
                                                'value': filter_value}
            if meta['terms']['field'].endswith('.uri'):
                for bucket in results['aggregations'][key]['buckets']:
                    aggregation_labels.add(bucket['key'])
                    bucket['value'] = contract(bucket['key'])
            else:
                for bucket in results['aggregations'][key]['buckets']:
                    bucket['value'] = bucket['key']

        labels = get_labels(list(map(rdflib.URIRef, aggregation_labels)), endpoint=self.endpoint)
        for key in query['aggregations']:
            if results['aggregations'][key]['meta']['terms']['field'].endswith('.uri'):
                for bucket in results['aggregations'][key]['buckets']:
                    uri = rdflib.URIRef(bucket['key'])
                    if uri in labels:
                        bucket['label'] = str(labels[uri])

        for hit in results['hits']['hits']:
            try:
                hit['_url'] = doc_forwards(hit['_source']['uri'])[None]
            except KeyError:
                raise

        return results

    def get_pagination(self, page_size, page, start, results):
        page_count = int(math.ceil(results['hits']['total'] / page_size))
        pages = set([1, page_count])
        pages.update(p for p in range(page-5, page+6) if 1 <= p <= page_count)
        pages = sorted(pages)
        
        pages_out = []
        for p in pages:
            if pages_out and pages_out[-1] != p - 1:
                pages_out.append(None)
            pages_out.append(p)
        
        return {'page_count': max(1, page_count),
                'start': start + 1,
                'pages': pages_out,
                'page': page,
                'page_size': page_size}

    @renderer(format="html", mimetypes=('text/html', 'application/xhtml+xml'), priority=1, name='HTML')
    def render_html(self, request, context, template_name):
        if 'hits' in context:
            self.strip_underscores(context['hits'])
        return super(SearchView, self).render_html(request, context, template_name)

    @renderer(format="autocomplete", name="Autocomplete JSON")
    def render_autocomplete(self, request, context, template_name):
        if not context.get('hits'):
            raise self.MissingQuery()
        context = [{'value': hit['_source']['uri'],
                    'altNames': '\t'.join(l for l in hit['_source'].get('altLabel', []) + hit['_source'].get('hiddenLabel', [])),
                    'label': hit['_source']['label']} for hit in context['hits']['hits']]
        content, mimetype = json.dumps(context), 'application/json'
        if 'callback' in request.GET:
            content, mimetype = [request.GET['callback'], '(', content, ');'], 'application/javascript'
        return HttpResponse(content, mimetype=mimetype)

    def error(self, request, exception, args, kwargs, status_code):
        if isinstance(exception, self.MissingQuery):
            return self.error_view(request,
                                   {'error': {'status_code': 400,
                                              'message': "Missing 'q' parameter."}},
                                   'elasticsearch/bad_request')
        else:
            return super(SearchView, self).error(request, exception, args, kwargs, status_code)
