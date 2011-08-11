import urllib

import urlparse
try:
    from urlparse import parse_qs
except ImportError:
    from cgi import parse_qs

import rdflib

from django.conf import settings
from django.core.urlresolvers import reverse, resolve

from django_hosts.reverse import get_host, reverse_crossdomain

from humfrey.utils.http import MediaType

def get_doc_view():
    # Don't ask
    urlconf = get_host('data').urlconf
    doc_view = resolve(reverse('doc-generic', urlconf=urlconf), urlconf=urlconf)[0]
    doc_view = doc_view.func_closure[1].cell_contents(**doc_view.func_closure[0].cell_contents)
    
    return doc_view
    
def doc_forwards(uri, renderers, graph=None, described=None):
    """
    Determines all doc URLs for a URI.

    graph is an rdflib.ConjunctiveGraph that can be checked for a description
    of uri. described is a ternary boolean.
    """

    format_names = set(renderer.format for renderer in renderers)

    urls = {}
    for id_prefix, doc_prefix, is_local in settings.ID_MAPPING:
        if uri.startswith(id_prefix):
            urls[None] = doc_prefix + uri[len(id_prefix):]
            for format in format_names:
                urls[format] = '%s.%s' % (urls[None], format)
            return urls
    if graph and not described and any(graph.triples((uri, None, None))):
        described = True

    if described == False:
        urls[None] = uri
        for format in format_names:
            urls[format] = uri
        return urls

    if described == True:
        view_name = 'doc-generic'
    else:
        view_name = 'desc'
    urls[None] = 'http:%s?%s' % (reverse_crossdomain('data', view_name),
                                 urllib.urlencode((('uri', uri.encode('utf-8')),)))
    for format in format_names:
        urls[format] = '%s&%s' % (urls[None],
                                  urllib.urlencode((('format', format),)))
    return urls


def get_format(request):
    renderers = get_doc_view().get_renderers(request)
    if renderers:
        return renderers[0].format

def doc_forward(uri, request=None, graph=None, described=None, format=None):
    if request and not format:
        format = get_format(request)

    return doc_forwards(uri, get_doc_view()._renderers_by_format.values(), graph, described)[format]

def doc_backward(url, request=None):
    from humfrey.desc.views import DocView
    if request:
        format = get_format(request)
    else:
        format = None
    parsed_url = urlparse.urlparse(url)
    query = parse_qs(parsed_url.query)
    if url.split(':', 1)[-1].split('?')[0] == reverse_crossdomain('data', 'doc-generic'):
        return rdflib.URIRef(query.get('uri', [None])[0]), query.get('format', [None])[0], False
    if url.rsplit('.', 1)[-1] in get_doc_view()._renderers_by_format:
        url, format = url.rsplit('.', 1)
    for id_prefix, doc_prefix, is_local in settings.ID_MAPPING:
        if url.startswith(doc_prefix):
            url = id_prefix + url[len(doc_prefix):]
            return rdflib.URIRef(url), format, is_local
    else:
        return None, None, None
