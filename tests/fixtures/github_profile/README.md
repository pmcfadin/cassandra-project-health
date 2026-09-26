# GitHub profile fixtures (issue #52)

Recorded from the real `GET https://api.github.com/users/{login}` endpoint
(unauthenticated), with all personal data replaced by synthetic values while
the exact response shape (keys, field order, types) is kept:

- `user_with_company.json` -- a normal profile with a non-null `company`
  field, based on the real shape returned for `octocat`.
- `user_no_company.json` -- a normal profile with `company: null` (the
  common case: most GitHub users never set this field).
- `user_not_found.json` -- the real 404 body GitHub returns for a
  nonexistent login (recorded verbatim; it carries no personal data).
- `rate_limit_error.json` -- GitHub's documented REST API rate-limit error
  body (`https://docs.github.com/rest/overview/rate-limits-for-the-rest-api`),
  returned with HTTP 403/429 and `X-RateLimit-Remaining: 0` /
  `X-RateLimit-Reset: <epoch>` headers.
