import json

from smpp.pdu_inspector import (detect_multipart, multipart_key,
                                MultipartMessage)
from twisted.internet.defer import inlineCallbacks, returnValue, succeed
from zope.interface import implements

from vumi import log
from vumi.config import (Config, ConfigDict, ConfigRegex, ConfigText,
                         ConfigInt, ConfigBool)
from vumi.message import TransportUserMessage
from vumi.transports.smpp.iprocessors import (
    IDeliveryReportProcessor, IDeliverShortMessageProcessor,
    ISubmitShortMessageProcessor)
from vumi.transports.smpp.smpp_utils import (unpacked_pdu_opts, detect_ussd)


class DeliveryReportProcessorConfig(Config):

    DELIVERY_REPORT_REGEX = (
        'id:(?P<id>\S{,65})'
        ' +sub:(?P<sub>...)'
        ' +dlvrd:(?P<dlvrd>...)'
        ' +submit date:(?P<submit_date>\d*)'
        ' +done date:(?P<done_date>\d*)'
        ' +stat:(?P<stat>[A-Z]{7})'
        ' +err:(?P<err>...)'
        ' +[Tt]ext:(?P<text>.{,20})'
        '.*'
    )

    DELIVERY_REPORT_STATUS_MAPPING = {
        # Output values should map to themselves:
        'delivered': 'delivered',
        'failed': 'failed',
        'pending': 'pending',
        # SMPP `message_state` values:
        'ENROUTE': 'pending',
        'DELIVERED': 'delivered',
        'EXPIRED': 'failed',
        'DELETED': 'failed',
        'UNDELIVERABLE': 'failed',
        'ACCEPTED': 'delivered',
        'UNKNOWN': 'pending',
        'REJECTED': 'failed',
        # From the most common regex-extracted format:
        'DELIVRD': 'delivered',
        'REJECTD': 'failed',
        # Currently we will accept this for Yo! TODO: investigate
        '0': 'delivered',
    }

    delivery_report_regex = ConfigRegex(
        'Regex to use for matching delivery reports',
        default=DELIVERY_REPORT_REGEX, static=True)
    delivery_report_status_mapping = ConfigDict(
        "Mapping from delivery report message state to "
        "(`delivered`, `failed`, `pending`)",
        static=True, default=DELIVERY_REPORT_STATUS_MAPPING)


class DeliveryReportProcessor(object):
    implements(IDeliveryReportProcessor)
    CONFIG_CLASS = DeliveryReportProcessorConfig

    STATUS_MAP = {
        1: 'ENROUTE',
        2: 'DELIVERED',
        3: 'EXPIRED',
        4: 'DELETED',
        5: 'UNDELIVERABLE',
        6: 'ACCEPTED',
        7: 'UNKNOWN',
        8: 'REJECTED',
    }

    def __init__(self, transport, config):
        self.transport = transport
        self.config = self.CONFIG_CLASS(config, static=True)

    def handle_delivery_report_pdu(self, pdu):
        """
        Check if this might be a delivery receipt with PDU parameters.

        There's a chance we'll get a delivery receipt without these
        parameters, if that happens we'll try a regex match in
        ``inspect_delivery_report_content`` once the message
        has (potentially) been reassembled and decoded.
        """
        pdu_opts = unpacked_pdu_opts(pdu)
        receipted_message_id = pdu_opts.get('receipted_message_id', None)
        message_state = pdu_opts.get('message_state', None)
        if receipted_message_id is None or message_state is None:
            return succeed(False)

        status = self.STATUS_MAP.get(message_state, 'UNKNOWN')

        d = self.transport.handle_delivery_report(
            receipted_message_id=receipted_message_id,
            delivery_status=self.delivery_status(status))
        d.addCallback(lambda _: True)
        return d

    def handle_delivery_report_content(self, content):
        delivery_report = self.config.delivery_report_regex.search(
            content or '')

        if not delivery_report:
            return succeed(False)

        # We have a delivery report.
        fields = delivery_report.groupdict()
        receipted_message_id = fields['id']
        message_state = fields['stat']
        d = self.transport.handle_delivery_report(
            receipted_message_id=receipted_message_id,
            delivery_status=self.delivery_status(message_state))
        d.addCallback(lambda _: True)
        return d

    def delivery_status(self, state):
        return self.config.delivery_report_status_mapping.get(state, 'pending')


class DeliverShortMessageProcessorConfig(Config):
    data_coding_overrides = ConfigDict(
        "Overrides for data_coding character set mapping. This is useful for "
        "setting the default encoding (0), adding additional undefined "
        "encodings (such as 4 or 8) or overriding encodings in cases where "
        "the SMSC is violating the spec (which happens a lot). Keys should "
        "be integers, values should be strings containing valid Python "
        "character encoding names.", default={}, static=True)


class DeliverShortMessageProcessor(object):
    """
    Messages can arrive with one of a number of specified
    encodings. We only handle a subset of these.

    From the SMPP spec:

    00000000 (0) SMSC Default Alphabet
    00000001 (1) IA5(CCITTT.50)/ASCII(ANSIX3.4)
    00000010 (2) Octet unspecified (8-bit binary)
    00000011 (3) Latin1(ISO-8859-1)
    00000100 (4) Octet unspecified (8-bit binary)
    00000101 (5) JIS(X0208-1990)
    00000110 (6) Cyrllic(ISO-8859-5)
    00000111 (7) Latin/Hebrew (ISO-8859-8)
    00001000 (8) UCS2(ISO/IEC-10646)
    00001001 (9) PictogramEncoding
    00001010 (10) ISO-2022-JP(MusicCodes)
    00001011 (11) reserved
    00001100 (12) reserved
    00001101 (13) Extended Kanji JIS(X 0212-1990)
    00001110 (14) KSC5601
    00001111 (15) reserved

    Particularly problematic are the "Octet unspecified" encodings.
    """

    implements(IDeliverShortMessageProcessor)
    CONFIG_CLASS = DeliverShortMessageProcessorConfig

    def __init__(self, transport, config):
        self.transport = transport
        self.redis = transport.redis
        self.codec = transport.get_static_config().codec_class()
        self.config = self.CONFIG_CLASS(config, static=True)

        self.data_coding_map = {
            1: 'ascii',
            3: 'latin1',
            # http://www.herongyang.com/Unicode/JIS-ISO-2022-JP-Encoding.html
            5: 'iso2022_jp',
            6: 'iso8859_5',
            7: 'iso8859_8',
            # Actually UCS-2, but close enough.
            8: 'utf-16be',
            # http://en.wikipedia.org/wiki/Short_Message_Peer-to-Peer
            9: 'shift_jis',
            10: 'iso2022_jp'
        }
        self.data_coding_map.update(self.config.data_coding_overrides)

    def dcs_decode(self, obj, data_coding):
        codec_name = self.data_coding_map.get(data_coding, None)
        if codec_name is None:
            log.msg("WARNING: Not decoding message with data_coding=%s" % (
                    data_coding,))
            return obj
        elif obj is None:
            log.msg(
                "WARNING: Not decoding `None` message with data_coding=%s" % (
                    data_coding,))
            return obj

        try:
            return self.codec.decode(obj, codec_name)
        except UnicodeDecodeError, e:
            log.msg("Error decoding message with data_coding=%s" % (
                data_coding,))
            log.err(e)
        return obj

    def decode_pdus(self, pdus):
        content = []
        for pdu in pdus:
            pdu_params = pdu['body']['mandatory_parameters']
            pdu_opts = unpacked_pdu_opts(pdu)

            # We might have a `message_payload` optional field to worry about.
            message_payload = pdu_opts.get('message_payload', None)
            if message_payload is not None:
                short_message = message_payload.decode('hex')
            else:
                short_message = pdu_params['short_message']

            content.append(
                self.dcs_decode(short_message, pdu_params['data_coding']))
        return content

    def handle_short_message_content(self, source_addr, destination_addr,
                                     short_message, **kw):
        return self.transport.handle_raw_inbound_message(
            source_addr=source_addr, destination_addr=destination_addr,
            short_message=short_message, **kw)

    def handle_short_message_pdu(self, pdu):
        pdu_params = pdu['body']['mandatory_parameters']
        content_parts = self.decode_pdus([pdu])
        if content_parts is not None:
            content = u''.join(content_parts)
        else:
            content = None

        d = self.handle_short_message_content(
            source_addr=pdu_params['source_addr'],
            destination_addr=pdu_params['destination_addr'],
            short_message=content)
        d.addCallback(lambda _: True)
        return d

    def handle_multipart_pdu(self, pdu):
        if not detect_multipart(pdu):
            return succeed(False)

        # We have a multipart SMS.
        pdu_params = pdu['body']['mandatory_parameters']
        d = self.handle_deliver_sm_multipart(pdu, pdu_params)
        d.addCallback(lambda _: True)
        return d

    @inlineCallbacks
    def handle_deliver_sm_multipart(self, pdu, pdu_params):
        redis_key = "multi_%s" % (multipart_key(detect_multipart(pdu)),)
        log.debug("Redis multipart key: %s" % (redis_key))
        multi = yield self.load_multipart_message(redis_key)
        multi.add_pdu(pdu)
        completed = multi.get_completed()
        if completed:
            yield self.redis.delete(redis_key)
            log.msg("Reassembled Message: %s" % (completed['message']))
            # We assume that all parts have the same data_coding here, because
            # otherwise there's nothing sensible we can do.
            decoded_msg = self.dcs_decode(completed['message'],
                                          pdu_params['data_coding'])
            # and we can finally pass the whole message on
            yield self.handle_short_message_content(
                source_addr=completed['from_msisdn'],
                destination_addr=completed['to_msisdn'],
                short_message=decoded_msg)
        else:
            yield self.save_multipart_message(redis_key, multi)

    def handle_ussd_pdu(self, pdu):
        pdu_params = pdu['body']['mandatory_parameters']
        pdu_opts = unpacked_pdu_opts(pdu)

        if not detect_ussd(pdu_opts):
            return succeed(False)

        # We have a USSD message.
        d = self.handle_deliver_sm_ussd(pdu, pdu_params, pdu_opts)
        d.addCallback(lambda _: True)
        return d

    def handle_deliver_sm_ussd(self, pdu, pdu_params, pdu_opts):
        # Some of this stuff might be specific to Tata's setup.

        service_op = pdu_opts['ussd_service_op']

        session_event = 'close'
        if service_op == '01':
            # PSSR request. Let's assume it means a new session.
            session_event = 'new'
        elif service_op == '11':
            # PSSR response. This means session end.
            session_event = 'close'
        elif service_op in ('02', '12'):
            # USSR request or response. I *think* we only get the latter.
            session_event = 'continue'

        # According to the spec, the first octet is the session id and the
        # second is the client dialog id (first 7 bits) and end session flag
        # (last bit).

        # Since we don't use the client dialog id and the spec says it's
        # ESME-defined, treat the whole thing as opaque "session info" that
        # gets passed back in reply messages.

        its_session_number = int(pdu_opts['its_session_info'], 16)
        end_session = bool(its_session_number % 2)
        session_info = "%04x" % (its_session_number & 0xfffe)

        if end_session:
            # We have an explicit "end session" flag.
            session_event = 'close'

        decoded_msg = self.dcs_decode(pdu_params['short_message'],
                                      pdu_params['data_coding'])
        return self.handle_short_message_content(
            source_addr=pdu_params['source_addr'],
            destination_addr=pdu_params['destination_addr'],
            short_message=decoded_msg,
            message_type='ussd',
            session_event=session_event,
            session_info=session_info)

    def _hex_for_redis(self, data_dict):
        for index, part in data_dict.items():
            part['part_message'] = part['part_message'].encode('hex')
        return data_dict

    def _unhex_from_redis(self, data_dict):
        for index, part in data_dict.items():
            part['part_message'] = part['part_message'].decode('hex')
        return data_dict

    @inlineCallbacks
    def load_multipart_message(self, redis_key):
        value = yield self.redis.get(redis_key)
        value = json.loads(value) if value else {}
        log.debug("Retrieved value: %s" % (repr(value)))
        returnValue(MultipartMessage(self._unhex_from_redis(value)))

    def save_multipart_message(self, redis_key, multipart_message):
        data_dict = self._hex_for_redis(multipart_message.get_array())
        return self.redis.set(redis_key, json.dumps(data_dict))


class SubmitShortMessageProcessorConfig(Config):
    submit_sm_encoding = ConfigText(
        'How to encode the SMS before putting on the wire', static=True,
        default='utf-8')
    submit_sm_data_coding = ConfigInt(
        'What data_coding value to tell the SMSC we\'re using when putting'
        'an SMS on the wire', static=True, default=0)
    send_long_messages = ConfigBool(
        "If `True`, messages longer than 254 characters will be sent in the "
        "`message_payload` optional field instead of the `short_message` "
        "field. Default is `False`, simply because that maintains previous "
        "behaviour.", default=False, static=True)
    send_multipart_sar = ConfigBool(
        "If `True`, messages longer than 140 bytes will be sent as a series "
        "of smaller messages with the sar_* parameters set. Default is "
        "`False`.", default=False, static=True)
    send_multipart_udh = ConfigBool(
        "If `True`, messages longer than 140 bytes will be sent as a series "
        "of smaller messages with the user data headers. Default is `False`.",
        default=False, static=True)

    def post_validate(self):
        long_message_params = (
            'send_long_messages', 'send_multipart_sar', 'send_multipart_udh')
        set_params = [p for p in long_message_params if getattr(self, p)]
        if len(set_params) > 1:
            params = ', '.join(set_params)
            self.raise_config_error(
                "The following parameters are mutually exclusive: %s" % params)


class SubmitShortMessageProcessor(object):
    implements(ISubmitShortMessageProcessor)
    CONFIG_CLASS = SubmitShortMessageProcessorConfig

    def __init__(self, transport, config):
        self.transport = transport
        self.config = self.CONFIG_CLASS(config, static=True)

    def handle_outbound_message(self, message, protocol):
        to_addr = message['to_addr']
        from_addr = message['from_addr']
        text = message['content']
        vumi_message_id = message['message_id']

        # TODO: this should probably be handled by a processor as these
        #       USSD fields & params are TATA (India) specific
        session_event = message['session_event']
        transport_type = message['transport_type']
        optional_parameters = {}

        if transport_type == 'ussd':
            continue_session = (
                session_event != TransportUserMessage.SESSION_CLOSE)
            session_info = message['transport_metadata'].get(
                'session_info', '0000')
            optional_parameters.update({
                'ussd_service_op': '02',
                'its_session_info': "%04x" % (
                    int(session_info, 16) + int(not continue_session))
            })

        if self.config.send_long_messages:
            return protocol.submit_sm_long(
                vumi_message_id,
                to_addr.encode('ascii'),
                long_message=text.encode(self.config.submit_sm_encoding),
                data_coding=self.config.submit_sm_data_coding,
                source_addr=from_addr.encode('ascii'),
                optional_parameters=optional_parameters,
            )

        elif self.config.send_multipart_sar:
            return protocol.submit_csm_sar(
                vumi_message_id,
                to_addr.encode('ascii'),
                short_message=text.encode(self.config.submit_sm_encoding),
                data_coding=self.config.submit_sm_data_coding,
                source_addr=from_addr.encode('ascii'),
                optional_parameters=optional_parameters,
            )

        elif self.config.send_multipart_udh:
            return protocol.submit_csm_udh(
                vumi_message_id,
                to_addr.encode('ascii'),
                short_message=text.encode(self.config.submit_sm_encoding),
                data_coding=self.config.submit_sm_data_coding,
                source_addr=from_addr.encode('ascii'),
                optional_parameters=optional_parameters,
            )

        return protocol.submit_sm(
            vumi_message_id,
            to_addr.encode('ascii'),
            short_message=text.encode(self.config.submit_sm_encoding),
            data_coding=self.config.submit_sm_data_coding,
            source_addr=from_addr.encode('ascii'),
            optional_parameters=optional_parameters,
        )
