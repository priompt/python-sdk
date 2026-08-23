"""Priompt Python adapter. Works with LangChain, LangGraph, or raw Python."""

import os
import threading
import time
from urllib.parse import urlparse

import grpc

from priompt.v1 import prompt_pb2, prompt_pb2_grpc


def _subject(uri):
    return "priompt." + uri.removeprefix("priompt://").replace("/", ".")


def _parse_url(raw):
    """Split a priompt://<token>@host:port connection URL into (host, token).
    A value without a scheme is treated as a bare host. Either part may be empty.
    """
    if "://" not in raw:
        return raw, None
    u = urlparse(raw)
    host = u.hostname or ""
    if u.port:
        host = f"{host}:{u.port}"
    return host, u.username


class PromptClient:
    def __init__(
        self,
        host=None,
        token=None,
        tls=False,
        ca_cert=None,
        client_cert=None,
        client_key=None,
        cache_ttl=0,
        nats_url=None,
        url=None,
    ):
        # One connection string covers local/self-host/cloud: pass url= or set
        # PRIOMPT_URL (priompt://<token>@host:port). An explicit host= wins;
        # an explicit token= overrides the URL's token.
        if host is None:
            raw = url or os.environ.get("PRIOMPT_URL")
            if raw:
                host, url_token = _parse_url(raw)
                if token is None:
                    token = url_token
        if not host:
            raise ValueError("PromptClient needs host=, url=, or PRIOMPT_URL")
        if tls:
            # ca_cert, client_cert and client_key are all *paths*.
            #
            # client_cert/client_key are what let an agent reach a server started
            # with -client-ca, which refuses connections without a certificate
            # signed by that CA — before authentication runs at all. Without them
            # this client simply could not talk to an mTLS deployment, which made
            # mTLS a CLI-only feature even though agents are the consumers it
            # exists to protect.
            if bool(client_cert) != bool(client_key):
                raise ValueError("client_cert and client_key must be given together")
            creds = grpc.ssl_channel_credentials(
                root_certificates=open(ca_cert, "rb").read() if ca_cert else None,
                private_key=open(client_key, "rb").read() if client_key else None,
                certificate_chain=open(client_cert, "rb").read() if client_cert else None,
            )
            self._chan = grpc.secure_channel(host, creds)
        else:
            self._chan = grpc.insecure_channel(host)
        self._stub = prompt_pb2_grpc.PromptServiceStub(self._chan)
        self._md = [("authorization", f"Bearer {token}")] if token else None
        # L1 (local) cache: key -> (value, expiry). cache_ttl=0 disables it.
        # Keys: a bare uri for get(); ("list", prefix) for list() so the two never
        # collide. The server only returns validated prompts, so anything cached is
        # valid; TTL is the staleness bound (a change shows up within cache_ttl s).
        self._cache_ttl = cache_ttl
        self._cache = {}
        self._nats_url = nats_url  # e.g. "nats://your-company.prompts.io:4222"

    def get(self, uri, ref=""):
        """Fetch a prompt by priompt:// URI. Returns the GetPromptResponse.

        Pass `ref` (a branch name or commit hash) to pin a specific version
        instead of the served HEAD; pinned reads are not cached.
        """
        if ref:
            return self._stub.GetPrompt(
                prompt_pb2.GetPromptRequest(uri=uri, ref=ref), metadata=self._md
            )
        if self._cache_ttl:
            hit = self._cache.get(uri)
            if hit and hit[1] > time.monotonic():
                return hit[0]
        resp = self._stub.GetPrompt(
            prompt_pb2.GetPromptRequest(uri=uri), metadata=self._md
        )
        if self._cache_ttl:
            self._cache[uri] = (resp, time.monotonic() + self._cache_ttl)
        return resp

    def list(self, prefix=""):
        """Browse a repo (a URI prefix) like a filesystem. Returns the entries
        (each with `uri` and `version_hash`) of every prompt whose URI starts with
        `prefix`, sorted. Empty prefix lists everything you're scoped to. Derive a
        tree by splitting each uri on "/".

        Cached client-side for cache_ttl seconds (like get); within that window a
        newly published prompt under `prefix` won't appear yet.
        """
        key = ("list", prefix)
        if self._cache_ttl:
            hit = self._cache.get(key)
            if hit and hit[1] > time.monotonic():
                return hit[0]
        entries = self._stub.ListPrompts(
            prompt_pb2.ListPromptsRequest(prefix=prefix), metadata=self._md
        ).entries
        if self._cache_ttl:
            self._cache[key] = (entries, time.monotonic() + self._cache_ttl)
        return entries

    def diff(self, uri, new_template):
        """Semantic Propagation Diff of the stored prompt at `uri` vs an edited
        template, computed server-side with the server's embedding model.
        Returns the DiffPromptResponse (a list of Change with the three signals).
        """
        return self._stub.DiffPrompt(
            prompt_pb2.DiffPromptRequest(uri=uri, new_template=new_template),
            metadata=self._md,
        )

    def subscribe(self, uri, on_change):
        """Register as a subscriber: call on_change(version_hash, classification)
        whenever the prompt at `uri` is republished (push). `classification` is
        the semantic diff verdict (structural | localized tweak | minor edit |
        new | ""), so an agent can auto-reload a tweak but hold a structural
        change. TTL polling via cache_ttl is the pull side. Returns the daemon
        thread running the NATS subscription.

        Requires `pip install nats-py` and nats_url set on the client.
        """
        if not self._nats_url:
            raise ValueError("set nats_url on PromptClient to subscribe")

        import asyncio
        import json

        import nats

        subject = _subject(uri)

        def run():
            async def main():
                nc = await nats.connect(self._nats_url)

                async def handler(msg):
                    self._cache.pop(uri, None)  # drop stale cache
                    self.get(uri)  # refetch now so the next get() is warm
                    try:
                        ev = json.loads(msg.data.decode())
                        on_change(ev.get("version", ""), ev.get("classification", ""))
                    except (ValueError, AttributeError):
                        on_change(msg.data.decode(), "")  # pre-0.7 bare-hash body

                await nc.subscribe(subject, cb=handler)
                while True:
                    await asyncio.sleep(3600)

            asyncio.run(main())

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def close(self):
        self._chan.close()
