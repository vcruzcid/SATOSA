import json
import time

from jwkest.jwk import RSAKey
from oic.oauth2 import rndstr
from oic.oic import DEF_SIGN_ALG
from oic.oic.consumer import Consumer
from oic.oic.message import RegistrationResponse, RegistrationRequest, AccessTokenRequest, \
    AuthorizationRequest, AuthorizationResponse, UserInfoRequest
from oic.oic.provider import Provider
from oic.utils.authn.authn_context import AuthnBroker
from oic.utils.authn.client import verify_client
from oic.utils.authn.user import UserAuthnMethod
from oic.utils.authz import AuthzHandling
from oic.utils.http_util import Response
from oic.utils.keyio import KeyJar, KeyBundle, UnknownKeyType

from oic.utils.sdb import SessionDB, AuthnEvent

from oic.utils.userinfo import UserInfo

from oic.utils.webfinger import WebFinger
import responses

from satosa.context import Context
from satosa.state import State
from tests.util import FileGenerator
from six.moves.urllib.parse import urlparse

__author__ = 'danielevertsson'


def keybundle_from_local_file(filename, typ, usage, kid):
    if typ.upper() == "RSA":
        kb = KeyBundle()
        k = RSAKey(kid=kid)
        k.load(filename)
        k.use = usage[0]
        kb.append(k)
        for use in usage[1:]:
            _k = RSAKey(kid=kid + "1")
            _k.use = use
            _k.load_key(k.key)
            kb.append(_k)
    elif typ.lower() == "jwk":
        kb = KeyBundle(source=filename, fileformat="jwk", keyusage=usage)
    else:
        raise UnknownKeyType("Unsupported key type")
    return kb


class DummyAuthn(UserAuthnMethod):
    def __init__(self, srv, user):
        UserAuthnMethod.__init__(self, srv)
        self.user = user

    def authenticated_as(self, cookie=None, **kwargs):
        return {"uid": self.user}, time.time()


class RpConfig(object):
    def __init__(self, module_base):
        self.CLIENTS = {
            "": {
                "client_info": {
                    "application_type": "web",
                    "application_name": "SATOSA",
                    "contacts": ["ops@example.com"],
                    "redirect_uris": ["%sauthz_cb" % module_base],
                    "response_types": ["code"],
                    "subject_type": "pairwise"
                },
                "behaviour": {
                    "response_type": "code",
                    "scope": ["openid", "profile", "email", "address", "phone"],
                }
            }
        }
        self.ACR_VALUES = ["PASSWORD"]
        self.VERIFY_SSL = False
        self.OP_URL = "https://op.tester.se/"
        self.STATE_ENCRYPTION_KEY = "Qrn9IQ5hr9uUnIdNQe2e0KxsmR3CusyARs3RKLjp"
        self.STATE_ID = "OpenID_Qrn9R3Cus"


class TestConfiguration(object):
    """
    Testdata.

    The IdP and SP configuration is relying on endpoints with POST to simply the testing.
    """
    _instance = None

    def __init__(self):
        self.op_config = {}
        self.rp_base = "https://rp.example.com/openid/"
        self.rp_config = RpConfig(self.rp_base)

    @staticmethod
    def get_instance():
        """
        Returns an instance of the singleton class.
        """
        if not TestConfiguration._instance:
            TestConfiguration._instance = TestConfiguration()
        return TestConfiguration._instance


CLIENT_ID = "client_1"

_, idp_key_file = FileGenerator.get_instance().generate_cert("idp")
KC_RSA = keybundle_from_local_file(
    idp_key_file.name,
    "RSA",
    ["ver", "sig"],
    "op_sign"
)
KEYJAR = KeyJar()
KEYJAR[CLIENT_ID] = [KC_RSA]
KEYJAR[""] = KC_RSA
JWKS = KEYJAR.export_jwks()

CDB = {
    CLIENT_ID: {
        "client_secret": "client_secret",
        "redirect_uris": [("%sauthz" % TestConfiguration.get_instance().rp_base, None)],
        "client_salt": "salted"
    }
}

op_url = TestConfiguration.get_instance().rp_config.OP_URL

SERVER_INFO = {
    "version": "3.0",
    "issuer": op_url,
    "authorization_endpoint": "%sauthorization" % op_url,
    "token_endpoint": "%stoken" % op_url,
    "flows_supported": ["code", "token", "code token"],
}

CONSUMER_CONFIG = {
    "authz_page": "/authz",
    "scope": ["openid"],
    "response_type": ["code"],
    "user_info": {
        "name": None,
        "email": None,
        "nickname": None
    },
    "request_method": "param"
}

USERNAME = "username"
USERDB = {
    USERNAME: {
        "name": "Linda Lindgren",
        "nickname": "Linda",
        "email": "linda@example.com",
        "verified": True,
        "sub": "username"
    }
}

USERINFO = UserInfo(USERDB)

AUTHN_BROKER = AuthnBroker()
AUTHN_BROKER.add("UNDEFINED", DummyAuthn(None, USERNAME))

AUTHZ = AuthzHandling()
SYMKEY = rndstr(16)  # symmetric key used to encrypt cookie info


class FakeOP:
    def __init__(self):
        op_base_url = TestConfiguration.get_instance().rp_config.OP_URL
        self.provider = Provider(
            "pyoicserv",
            SessionDB(op_base_url),
            CDB,
            AUTHN_BROKER,
            USERINFO,
            AUTHZ,
            verify_client,
            SYMKEY,
            urlmap=None,
            keyjar=KEYJAR
        )
        self.provider.baseurl = TestConfiguration.get_instance().rp_config.OP_URL
        self.op_base = TestConfiguration.get_instance().rp_config.OP_URL
        self.redirect_urls = TestConfiguration.get_instance().rp_config.CLIENTS[""]["client_info"][
            "redirect_uris"]

    def setup_userinfo_endpoint(self):
        cons = Consumer({}, CONSUMER_CONFIG, {"client_id": CLIENT_ID},
                        server_info=SERVER_INFO, )
        cons.behaviour = {
            "request_object_signing_alg": DEF_SIGN_ALG["openid_request_object"]}
        cons.keyjar[""] = KC_RSA

        cons.client_secret = "drickyoughurt"
        cons.config["response_type"] = ["token"]
        cons.config["request_method"] = "parameter"
        state, location = cons.begin("openid", "token",
                                     path=TestConfiguration.get_instance().rp_base)

        resp = self.provider.authorization_endpoint(
            request=urlparse(location).query)

        # redirect
        atr = AuthorizationResponse().deserialize(
            urlparse(resp.message).fragment, "urlencoded")

        uir = UserInfoRequest(access_token=atr["access_token"], schema="openid")
        resp = self.provider.userinfo_endpoint(request=uir.to_urlencoded())
        responses.add(
            responses.POST,
            self.op_base + "userinfo_endpoint",
            body=resp.message,
            status=200,
            content_type='application/json')

    def setup_token_endpoint(self):
        authreq = AuthorizationRequest(state="state",
                                       redirect_uri=self.redirect_urls[0],
                                       client_id=CLIENT_ID,
                                       response_type="code",
                                       scope=["openid"])
        _sdb = self.provider.sdb
        sid = _sdb.token.key(user="sub", areq=authreq)
        access_grant = _sdb.token(sid=sid)
        ae = AuthnEvent("user", "salt")
        _sdb[sid] = {
            "oauth_state": "authz",
            "authn_event": ae,
            "authzreq": authreq.to_json(),
            "client_id": CLIENT_ID,
            "code": access_grant,
            "code_used": False,
            "scope": ["openid"],
            "redirect_uri": self.redirect_urls[0],
        }
        _sdb.do_sub(sid, "client_salt")
        # Construct Access token request
        areq = AccessTokenRequest(code=access_grant, client_id=CLIENT_ID,
                                  redirect_uri=self.redirect_urls[0],
                                  client_secret="client_secret_1")
        txt = areq.to_urlencoded()
        resp = self.provider.token_endpoint(request=txt)
        responses.add(
            responses.POST,
            self.op_base + "token",
            body=resp.message,
            status=200,
            content_type='application/json')

    def setup_authentication_response(self):
        context = Context()
        context.path = 'openid/authz_cb'
        op_base = TestConfiguration.get_instance().rp_config.OP_URL
        context.request = {
            'code': 'F+R4uWbN46U+Bq9moQPC4lEvRd2De4o=',
            'scope': 'openid profile email address phone', 'state': self.generate_state(op_base)}
        return context

    def generate_state(self, op_base):
        state = State()
        state_id = TestConfiguration.get_instance().rp_config.STATE_ID
        state_data = {
            "op": op_base,
            "nonce": "9YraWpJAmVp4L3NJ"
        }
        state.add(state_id, state_data)
        encryption_key = TestConfiguration.get_instance().rp_config.STATE_ENCRYPTION_KEY
        return state.urlstate(encryption_key)

    def setup_client_registration_endpoint(self):
        client_info = TestConfiguration.get_instance().rp_config.CLIENTS[""]["client_info"]
        request = RegistrationRequest().deserialize(json.dumps(client_info), "json")
        _cinfo = self.provider.do_client_registration(request, CLIENT_ID)
        args = dict([(k, v) for k, v in _cinfo.items()
                     if k in RegistrationResponse.c_param])
        args['client_id'] = CLIENT_ID
        self.provider.comb_uri(args)
        registration_response = RegistrationResponse(**args)
        responses.add(
            responses.POST,
            self.op_base + "registration",
            body=registration_response.to_json(),
            status=200,
            content_type='application/json')

    def setup_opienid_config_endpoint(self):
        self.provider.baseurl = self.op_base
        provider_info = self.provider.create_providerinfo()
        responses.add(
            responses.GET,
            self.op_base + ".well-known/openid-configuration",
            body=provider_info.to_json(),
            status=200,
            content_type='application/json'
        )

    def setup_webfinger_endpoint(self):
        wf = WebFinger()
        resp = Response(wf.response(subject=self.op_base, base=self.op_base))
        responses.add(responses.GET,
                      self.op_base + ".well-known/webfinger",
                      body=resp.message,
                      status=200,
                      content_type='application/json')

    def publish_jwks(self):
        responses.add(
            responses.GET,
            self.op_base + "static/jwks.json",
            body=json.dumps(JWKS),
            status=200,
            content_type='application/json')