import itertools

import mock
import rdflib, rdflib.term
import unittest2

from django.test.client import Client
from django.http import HttpResponseNotFound

from humfrey.desc import rdf_processors, views
from humfrey.utils import sparql, resource
from humfrey.tests.stubs import stub_reverse_crossdomain

class GraphTestMixin(object):
    def check_valid_terms(self, graph):
        for term in itertools.chain.from_iterable(graph):
            self.assertIsInstance(term, rdflib.term.Identifier)

@mock.patch('humfrey.linkeddata.uri.reverse_crossdomain', stub_reverse_crossdomain)
class RDFProcessorsTestCase(unittest2.TestCase, GraphTestMixin):
    _ALL = [
        rdf_processors.formats,
        rdf_processors.doc_meta,
    ]

    def testAll(self):
        for rdf_processor in self._ALL:
            endpoint = mock.Mock(spec=sparql.Endpoint)
            graph = rdflib.ConjunctiveGraph()
            doc_uri = rdflib.URIRef('http://example.com/doc/Foo')
            subject_uri = rdflib.URIRef('http://example.com/id/Foo')
            subject = resource.Resource(subject_uri, graph, endpoint)
            renderers = views.DocView().FORMATS.values()

            context = rdf_processor(graph, doc_uri, subject_uri, subject, endpoint, renderers)

            self.assertFalse(endpoint.query.called, "The rdf procesor should not be touching the endpoint (at the moment)")
            self.check_valid_terms(graph)
            self.assertIsInstance(context, (type(None), dict))

#@mock.patch('humfrey.linkeddata.uri.reverse_crossdomain', stub_reverse_crossdomain)
class DocViewTestCase(unittest2.TestCase, GraphTestMixin):
    _TEST_URI = 'http://data/example.com/id/Foo'
    _HTTP_HOST = 'data.example.org'

    def setUp(self):
        self.client = Client()

    @mock.patch('humfrey.desc.views.DocView.get_types')
    def testNoTypes(self, get_types):
        get_types.return_value = ()
        response = self.client.get('/doc/', {'uri': self._TEST_URI}, HTTP_HOST=self._HTTP_HOST)
        self.assertIsInstance(response, HttpResponseNotFound)


    @mock.patch('humfrey.desc.views.DocView.get_types')
    @mock.patch('humfrey.desc.views.DocView.endpoint')
    def testGraphValid(self, endpoint, get_types):
        get_types.return_value = (rdflib.URIRef('http://example.org/vocab/Thing'),)
        endpoint.query.return_value = rdflib.ConjunctiveGraph()
        response = self.client.get('/doc/', {'uri': self._TEST_URI, 'format': 'html'}, HTTP_HOST='data.example.org')
        self.assertIsInstance(response.context['subject_uri'], rdflib.URIRef)
        self.assertIsInstance(response.context['doc_uri'], rdflib.URIRef)
        self.check_valid_terms(response.context['graph'])

