# Authorization and account review

In YouTube → Reconnect, click **Create authorization link**. The same popup shows progress and then **Open Google sign-in**, **Copy sign-in link**, and **Open private window**. Choose the intended account and exact channel in Google. Rejected requests and expired sign-in attempts appear inline, with the button available to try again. A hidden connection ID no longer interferes with form submission.

Dashboard **Review** shows which connection needs attention. GitHub account suspension is distinguished from rate limits, expired tokens, and missing permissions. Arbitrary provider response bodies are never displayed.

An **Account suspended** notice links to [GitHub Appeal and Reinstatement](https://docs.github.com/en/site-policy/acceptable-use-policies/github-appeal-and-reinstatement). Reconnecting a token cannot lift a GitHub suspension. Review and health checks preserve saved routes and ledgers and do not start or migrate workers. A stale successful health result cannot mark a currently failed connection healthy.

Verification uses intercepted browser requests and temporary test workspaces. No real Google consent or publishing is performed by the regression tests.
