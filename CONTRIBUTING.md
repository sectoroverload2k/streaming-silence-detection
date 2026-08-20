# Contributing

Bug reports, stream compatibility reports and pull requests are all welcome.

## Getting set up

Everything is stdlib Python and a shell script, so there is nothing to install
beyond Docker and ffmpeg.

```
docker build -t silence-detect:local .
IMAGE=silence-detect:local ci/smoke/run.sh
```

The smoke test generates a tone → silence → tone file, serves it over HTTP
behind basic auth, and asserts that the detector POSTs the expected events to a
stub webhook from inside the image. It must print `ALL_SMOKE_CHECKS_OK` before
you open a pull request.

To try the detector against a real stream without a webhook receiver, leave
`WEBHOOK_URL` unset and every event is logged to stdout as the JSON that would
have been sent.

## House rules

- **Standard library only.** No pip dependencies — it keeps the image small and
  the attack surface honest. If something genuinely needs a dependency, open an
  issue first.
- **Keep ffmpeg at arm's length.** It is invoked as a subprocess, never linked.
  That boundary is what keeps its GPL licence off this codebase.
- **Never log or send credentials.** `Monitor.scrub()` strips the stream URL and
  password out of every ffmpeg line before it is logged or put in a webhook
  payload. If you add a new path that surfaces ffmpeg output, scrub it.
- One change per pull request, and say what you tested it against.

## Contribution terms

This project is released under the [Elastic License 2.0](LICENSE), and the
maintainer also offers it under separate commercial terms. That dual
arrangement only works if the maintainer holds the necessary rights to every
line in the repository — so contributions need an explicit grant.

**By opening a pull request, you confirm that:**

1. The contribution is your own work, or you otherwise have the right to submit
   it under these terms. If your employer has rights to work you produce, you
   have their permission to contribute.

2. You grant Anthony Linsday a perpetual, worldwide, non-exclusive, royalty-free
   and irrevocable licence to use, reproduce, modify, prepare derivative works
   of, publicly display, sublicense and distribute your contribution — **and to
   license it under terms other than the Elastic License 2.0, including
   commercial terms.**

3. You grant, under any patent claims you can license, a perpetual, worldwide,
   non-exclusive, royalty-free and irrevocable patent licence to make, use,
   sell, offer to sell, import and otherwise transfer your contribution, alone
   or combined with this project.

4. Your contribution is provided as is, without warranty of any kind.

**You keep your copyright.** This is a licence, not an assignment — you remain
free to use your own contribution however you like, anywhere else.

A signed CLA, checked automatically on each pull request, will replace this
section once the project takes contributions regularly. Until then this notice
is the record of the terms your pull request is offered under; if you are not
willing to contribute on these terms, please open an issue to discuss it rather
than sending a pull request.
