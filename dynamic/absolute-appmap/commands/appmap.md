---
description: Start an absolute-appmap run - crawl and map a running web application (config file or base URL).
argument-hint: <appmap.config.yaml | base URL>
---

Map a running web application with the absolute-appmap plugin. Use the `map-running-application` skill and follow it from Step 0.

Input: $ARGUMENTS

- If the input is a path to an `appmap.config.yaml`, review that config with the operator in the `configure-crawl` skill (authorization and scope must still be confirmed explicitly by the operator), then continue the loop.
- If the input is a base URL (or empty), run `configure-crawl` from the beginning: confirm authorization and scope with the operator, interview them, write the config, and open the run with `appmap init`.

Do not send any request to the target before the operator has confirmed authorization and scope.
