# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | yes |
| < 1.0 | no |

## Reporting a vulnerability

Email **security@kestrelimaging.example** with:

- what you found, and where (file, endpoint or plug-in command),
- the smallest reproduction you can manage,
- the impact you believe it has.

Please do not open a public issue, and please do not attach photographs you do not have the right to share.
We acknowledge within three business days and aim to ship a fix or a mitigation within thirty days for anything
that affects confidentiality, integrity or a customer's files.

## Threat model

Kestrel is a local application. The interesting boundaries are:

| Boundary | Design |
|---|---|
| Network | The engine binds to `0.0.0.0` by default for convenience on a workstation LAN. There is no authentication layer — **bind to `127.0.0.1` on a shared network.** No request leaves the machine on its own. |
| Files | Originals are opened read-only. Writes are confined to `KESTREL_HOME` and the Lightroom catalogue. |
| Uploads | `POST /api/import/upload` writes into the import directory with the filename sanitised to its basename; only known image extensions are accepted. |
| Plug-in | Talks to `127.0.0.1` over HTTP. Settings and snapshots live in Lightroom's own preferences. Develop values are whitelisted and clamped before writing. |
| Licence | Offline HMAC verification. Not DRM; see the licence for the honest statement of what it does. |
| Secrets | No credentials are stored. The licence key is written with mode `600`. |
| Telemetry | None. There is no analytics call in the codebase. |

## Hardening recommendations

```bash
kestrel serve --host 127.0.0.1
```

Run this on any machine that is not exclusively yours, and keep `KESTREL_HOME` on an encrypted disk if the library
database records paths to sensitive client work.
