# Priompt Python client

Fetch versioned prompts from a [Priompt](https://github.com/) server over gRPC.

```sh
pip install <dist-name>          # dist name TBD; imports as `priompt`
```

```python
from priompt import PromptClient

client = PromptClient(host="localhost:8443")            # token=... if auth is on
prompt = client.get("priompt://acme/onboarding/welcome")

print(prompt.template)                                  # Hi {name}, welcome to {org}!
text = prompt.template.format(name="Sujal", org="Acme")
```

`PromptClient(host, token=None, tls=False, ca_cert=None, cache_ttl=0, nats_url=None)`.
Methods: `get(uri, ref="")`, `diff(uri, new_template)`, `subscribe(uri, on_change)`.

`subscribe()` needs the `nats` extra: `pip install "<dist-name>[nats]"`.
