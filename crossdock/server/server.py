import logging

import tornado.web
import opentracing
import tornado.ioloop
import tornado.escape
import tornado.httpclient
from tornado.web import asynchronous
from jaeger_client import Tracer, ConstSampler
from jaeger_client.reporter import NullReporter
import crossdock.server.constants as constants
import crossdock.server.serializer as serializer
from opentracing_instrumentation import http_client, http_server, get_current_span, request_context
from opentracing_instrumentation.client_hooks import tornado_http
import opentracing.ext.tags as ext_tags
from crossdock.thrift_gen.tracetest.ttypes import ObservedSpan, TraceResponse, Transport, \
    JoinTraceRequest

from tchannel import TChannel, thrift
from crossdock.server.thriftrw_serializer import trace_response_to_thriftrw, \
    join_trace_request_to_thriftrw

DefaultClientPortHTTP = 8080
DefaultServerPortHTTP = 8081
DefaultServerPortTChannel = 8082
tchannel = None


tracer = Tracer(
    service_name='python',
    reporter=NullReporter(),
    sampler=ConstSampler(decision=True))
opentracing.tracer = tracer


idl_path = 'idl/thrift/crossdock/tracetest.thrift'
thrift_services = {}


def get_thrift_service(service_name):
    if service_name in thrift_services:
        return thrift_services[service_name]
    thrift_service = thrift.load(path=idl_path, service=service_name)
    thrift_services[service_name] = thrift_service
    return thrift_service


def serve():
    """main entry point"""
    logging.getLogger().setLevel(logging.DEBUG)
    logging.info('Python Tornado Crossdock Server Running ...')
    tchannel = make_tchannel(DefaultServerPortTChannel)
    tchannel.listen()
    app = make_app(Server())
    app.listen(DefaultClientPortHTTP)
    app.listen(DefaultServerPortHTTP)
    tornado.ioloop.IOLoop.current().start()


# Tornado Stuff
class MainHandler(tornado.web.RequestHandler):
    def initialize(self, server=None, method=None):
        self.server = server
        self.method = method

    @asynchronous
    def get(self):
        if self.server and self.method:
            self.method(self.server, self.request, self)
        else:
            self.finish()

    @asynchronous
    def post(self):
        if self.server and self.method:
            self.method(self.server, self.request, self)
        else:
            self.finish()

    def head(self):
        pass


def make_app(server):
    return tornado.web.Application(
        [
            (r'/', MainHandler),
            (r'/start_trace', MainHandler, (dict(server=server, method=Server.start_trace))),
            (r'/join_trace', MainHandler, (dict(server=server, method=Server.join_trace))),
        ], debug=True)


# TChannel handlers
def tchannelNotImplResponse():
    return TraceResponse(notImplementedError='python <=> tchannel not implemented')


def make_tchannel(port):
    global tchannel
    tchannel = TChannel('python', hostport='localhost:%d' % port, trace=True)

    service = get_thrift_service(service_name='python')

    @tchannel.thrift.register(service.TracedService, method='joinTrace')
    @tornado.gen.coroutine
    def join_trace(request):
        join_trace_request = request.body.request or None
        response = yield handle_downstream_request(join_trace_request.downstream)
        raise tornado.gen.Return(response)

    @tornado.gen.coroutine
    def handle_downstream_request(downstream):
        span = get_current_span()
        observed_span = service.ObservedSpan(
            traceId='%x' % span.trace_id,
            sampled=span.is_sampled(),
            baggage=span.get_baggage_item(constants.baggage_key)
        )

        trace_response = TraceResponse(span=observed_span, notImplementedError='')

        if downstream:
            downstream_trace_resp = yield call_downstream(span, downstream)
            observed_span = service.ObservedSpan(
                traceId=downstream_trace_resp.span.traceId,
                sampled=downstream_trace_resp.span.sampled,
                baggage=downstream_trace_resp.span.baggage
            )
            downstream_trace_resp = TraceResponse(span=observed_span, notImplementedError='')
            downstream_trace_resp = trace_response_to_thriftrw(service, downstream_trace_resp)
            trace_response.downstream = downstream_trace_resp

        raise tornado.gen.Return(trace_response_to_thriftrw(service, trace_response))

    return tchannel


# HTTP Tracing Stuff
class UnknownTransportException(Exception):
    pass


class Server(object):
    def __init__(self):
        self.tracer = opentracing.tracer

    @tornado.gen.coroutine
    def start_trace(self, request, response_writer):
        start_trace_req = serializer.start_trace_request_from_json(request.body)

        def update_span(span):
            span.set_baggage_item(constants.baggage_key, start_trace_req.baggage)
            span.set_tag(ext_tags.SAMPLING_PRIORITY, start_trace_req.sampled)

        self.handle_downstream_request(request, start_trace_req.downstream, update_span, response_writer)

    @tornado.gen.coroutine
    def join_trace(self, request, response_writer):

        jtr = serializer.join_trace_request_from_json(request.body) if request.body else None

        self.handle_downstream_request(request, jtr.downstream, None, response_writer)

    @tornado.gen.coroutine
    def handle_downstream_request(self, http_request, downstream, span_handler, response_writer):

        span = http_server.before_request(http_server.TornadoRequestWrapper(request=http_request),
                                          self.tracer)
        if span_handler:
            span_handler(span)

        with request_context.span_in_stack_context(span):
            trace_id = '%x' % span.trace_id
            observed_span = ObservedSpan(
                trace_id, span.is_sampled(),
                span.get_baggage_item(constants.baggage_key))

            trace_response = TraceResponse(span=observed_span)

            if downstream:

                def handle_response(future):
                    downstream_trace_resp = future.result()
                    trace_response.downstream = downstream_trace_resp
                    response_writer.write(serializer.obj_to_json(trace_response))
                    response_writer.finish()

                future = call_downstream(span, downstream)
                tornado.ioloop.IOLoop.current().add_future(future, handle_response)
            else:
                response_writer.write(serializer.obj_to_json(trace_response))
                response_writer.finish()


def get_observed_span(span):
    return ObservedSpan(
        traceId='%x' % span.trace_id,
        sampled=span.is_sampled(),
        baggage=span.get_baggage_item(constants.baggage_key)
    )


@tornado.gen.coroutine
def call_downstream(span, downstream):
    if downstream.transport == Transport.HTTP:
        downstream_trace_resp = yield call_downstream_http(span, downstream)
    elif downstream.transport == Transport.TCHANNEL:
        downstream_trace_resp = yield call_downstream_tchannel(downstream)
    else:
        raise UnknownTransportException('%s' % downstream.transport)
    raise tornado.gen.Return(downstream_trace_resp)


@tornado.gen.coroutine
def call_downstream_http(span, downstream):
    url = 'http://%s:%s/join_trace' % (downstream.host, downstream.port)
    body = serializer.join_trace_request_to_json(downstream.downstream, downstream.serverRole)

    req = tornado.httpclient.HTTPRequest(url=url, method='POST',
                                         headers={'Content-Type': 'application/json'},
                                         body=body)
    http_client.before_http_request(tornado_http.TornadoRequestWrapper(request=req),
                                    lambda: span)
    client = tornado.httpclient.AsyncHTTPClient()
    http_result = yield client.fetch(req)

    raise tornado.gen.Return(serializer.traceresponse_from_json(http_result.body))


@tornado.gen.coroutine
def call_downstream_tchannel(downstream):
    downstream_service = get_thrift_service(downstream.serviceName)

    jtr = JoinTraceRequest(downstream.serverRole, downstream.downstream)
    jtr = join_trace_request_to_thriftrw(downstream_service, jtr)

    thrift_result = yield tchannel.thrift(downstream_service.TracedService.joinTrace(jtr),
                                   hostport='localhost:%s' % downstream.port)
    raise tornado.gen.Return(thrift_result.body)
