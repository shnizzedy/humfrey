import time
import urllib2
from urlparse import urlparse

from lxml import etree
import rdflib
import redis

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect
from django.utils.importlib import import_module
from django.core.exceptions import ImproperlyConfigured

from humfrey.linkeddata.views import EndpointView, RDFView, ResultSetView
from humfrey.linkeddata.uri import doc_forward, doc_backward

from humfrey.utils.views import BaseView
from humfrey.utils.http import HttpResponseSeeOther, HttpResponseTemporaryRedirect
from humfrey.utils.resource import Resource, get_describe_query
from humfrey.utils.namespaces import NS
from humfrey.utils.cache import cached_view

from humfrey.desc.forms import SparqlQueryForm


class IndexView(BaseView):
    @cached_view
    def handle_GET(self, request, context):
        return self.render(request, context, 'index')

class IdView(EndpointView):
    def initial_context(self, request):
        uri = rdflib.URIRef(request.build_absolute_uri())
        if not self.get_types(uri):
            raise Http404
        return {
           'uri': uri,
           'description_url': doc_forward(uri, request, described=True),
        }

    @cached_view
    def handle_GET(self, request, context):
        return HttpResponseSeeOther(context['description_url'])

class DescView(EndpointView):
    """
    Will redirect to DocView if described by endpoint, otherwise to the URI given.

    Allows us to be lazy when determining whether to go on- or off-site.
    """
    def handle_GET(self, request, context):
        uri = rdflib.URIRef(request.GET.get('uri', ''))
        try:
            url = urlparse(uri)
        except Exception:
            raise Http404
        if self.get_types(uri):
            return HttpResponsePermanentRedirect(doc_forward(uri, request, described=True))
        elif url.scheme in ('http', 'https') and url.netloc and url.path.startswith('/'):
            return HttpResponseTemporaryRedirect(unicode(uri))
        else:
            raise Http404

class DocView(EndpointView, RDFView):

    def initial_context(self, request):
        doc_url = request.build_absolute_uri()

        uri, format, is_local = doc_backward(doc_url, request)
        if not uri:
            raise Http404
        format = format or 'html'

        expected_doc_url = doc_forward(uri, request, format=format, described=True)

        types = self.get_types(uri)
        if not types:
            raise Http404

        if expected_doc_url != doc_url:
            return HttpResponsePermanentRedirect(expected_doc_url)

        doc_uri = rdflib.URIRef(doc_forward(uri, request, format=None, described=True))

        return {
            'subject_uri': uri,
            'doc_uri': doc_uri,
            'format': format,
            'types': types,
            'show_follow_link': not is_local,
            'no_index': not is_local,
        }

    @cached_view
    def handle_GET(self, request, context):
        if isinstance(context, HttpResponse):
            return context
        subject_uri, doc_uri = context['subject_uri'], context['doc_uri']
        types = context['types']

        queries, graph = [], rdflib.ConjunctiveGraph()
        for prefix, ns in NS.iteritems():
            graph.namespace_manager.bind(prefix, ns)

        graph += ((subject_uri, NS.rdf.type, t) for t in types)
        subject = Resource(subject_uri, graph, self.endpoint)

        for query in subject.get_queries():
            graph += self.endpoint.query(query)
            queries.append(query)

        licenses, datasets = set(), set()
        for graph_name in graph.subjects(NS['ov'].describes):
            graph.add((doc_uri, NS['dcterms'].source, graph_name))
            licenses.update(graph.objects(graph_name, NS['dcterms'].license))
            datasets.update(graph.objects(graph_name, NS['void'].inDataset))
            
        if len(licenses) == 1:
            for license in licenses:
                graph.add((doc_uri, NS['dcterms'].license, license))

        if not graph:
            raise Http404

        for doc_rdf_processor in self._doc_rdf_processors:
            additional_context = doc_rdf_processor(graph=graph,
                                                   doc_uri=doc_uri,
                                                   subject_uri=subject_uri,
                                                   subject=subject,
                                                   endpoint=self.endpoint,
                                                   renderers=self.FORMATS.values())
            if additional_context:
                context.update(additional_context)
        
        context.update({
            'graph': graph,
            'subject': subject,
            'licenses': [Resource(uri, graph, self.endpoint) for uri in licenses],
            'datasets': [Resource(uri, graph, self.endpoint) for uri in datasets],
            'queries': queries,
        })

        if context['format']:
            try:
                return self.render_to_format(request, context, context['subject'].template_name, context['format'])
            except KeyError:
                raise Http404
        else:
            return self.render(request, context, context['subject'].template_name)

    @property
    def _doc_rdf_processors(self):
        if hasattr(self, '_doc_rdf_processors_cache'):
            return self._doc_rdf_processors_cache
        processors = []
        for name in settings.DOC_RDF_PROCESSORS:
            module_name, attribute_name = name.rsplit('.', 1)
            try:
                module = import_module(module_name)
            except ImportError, e:
                raise ImproperlyConfigured('Error importing doc RDF processor module %s: "%s"' % (module_name, e))
            try:
                processors.append(getattr(module, attribute_name))
            except AttributeError:
                raise ImproperlyConfigured('Module "%s" does not define a "%s" callable doc RDF processor' % (module_name, attribute_name))
        self._doc_rdf_processors_cache = tuple(processors)
        return self._doc_rdf_processors_cache



class SparqlView(EndpointView, RDFView, ResultSetView):
    class SparqlViewException(Exception): pass
    class ConcurrentQueryException(SparqlViewException): pass
    class ExcessiveQueryException(SparqlViewException): pass

    def perform_query(self, request, query, common_prefixes):
        client = redis.client.Redis(**settings.REDIS_PARAMS)
        addr = request.META['REMOTE_ADDR']
        if not client.setnx('sparql:lock:%s' % addr, 1):
            raise self.ConcurrentQueryException
        try:
            intensity = float(client.get('sparql:intensity:%s' % addr) or 0)
            last = float(client.get('sparql:last:%s' % addr) or 0)
            intensity = max(0, intensity - (time.time() - last) / 20)
            if intensity > 20:
                raise self.ExcessiveQueryException
            elif intensity > 10:
                time.sleep(intensity - 10)

            start = time.time()
            results = self.endpoint.query(query, timeout=5, common_prefixes=common_prefixes)
            end = time.time()

            client.set('sparql:intensity:%s' % addr, intensity + end - start)
            client.set('sparql:last:%s' % addr, end)

            return results
        finally:
            client.delete('sparql:lock:%s' % addr)
    
    def initial_context(self, request):
        query = request.REQUEST.get('query')
        data = dict(request.REQUEST.items())
        if not 'format' in data:
            data['format'] = 'html'
        form = SparqlQueryForm(data if query else None, formats=self.FORMATS)
        context = {
            'namespaces': NS,
            'form': form,
        }
        
        if not form.is_valid():
            return context

        try:        
            results = self.perform_query(request, query, form.cleaned_data['common_prefixes'])
        except urllib2.HTTPError, e:
            context['error'] = e.read() #parse(e).find('.//pre').text
            context['status_code'] = e.code
            return context
        except self.ConcurrentQueryException, e:
            context['error'] = "You cannot perform more than one query at a time.\nPlease wait for your previous query to complete or time out first."
            context['status_code'] = 403
            return context
        except self.ExcessiveQueryException, e:
            context['error'] = "You have been performing a lot of queries recently.\nPlease wait a while and try again."
            context['status_code'] = 403
            return context
        except etree.XMLSyntaxError, e:
            context['error'] = "Your query could not be returned in the time allotted it.\nPlease try a simpler query or using LIMIT to reduce the number of returned results."
            context['status_code'] = 403
            return context

        
        if isinstance(results, list):
            context['results'] = results
        elif isinstance(results, bool):
            context['result'] = results
        elif isinstance(results, rdflib.ConjunctiveGraph):
            context['graph'] = results
            context['subjects'] = results.subjects()

        context['queries'] = [results.query]
        context['duration'] = results.duration
        
        return context
    
    def handle_GET(self, request, context):
        return self.render(request, context, 'sparql')
    handle_POST = handle_GET
        
#class GraphView(BaseView):
#    def handle_GET(self, request, context):
#        req = urllib2.Request(settings.GRAPH_URL + '?' + urllib.urlencode({'graph': request.build_absolute_uri()}))
#        for header in request.META:
#            if header.startswith('
#            req.headers[header] = request.headers[header]
#            
#        try:
#            resp = urllib2.urlopen(req)
#        except urllib2.HTTPError, e:
#            resp = e
#        response = HttpResponse(response, status_code=e.code)
#        
#        return response

