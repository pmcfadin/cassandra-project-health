# GitHub commit-author GraphQL fixtures (issue #52 fixup cycle 1)

Recorded from a real query against `apache/cassandra`'s `trunk` branch
(`repository(owner:"apache", name:"cassandra"){ ref(qualifiedName:"trunk")
{ target { ... on Commit { history(first: 5) { pageInfo { hasNextPage
endCursor } nodes { oid author { email user { login } } } } } } } }`,
2026-09-25), with commit shas, emails and logins replaced by synthetic
values while the exact response shape (keys, nesting, `rateLimit` field,
`pageInfo` cursor format) is kept:

- `history_page1.json` -- first page, `hasNextPage: true`, includes one
  commit whose author has no linked GitHub account (`author.user: null`,
  a real, observed case) and one whose author email domain would never
  resolve via `org_domains.yaml` (a personal-looking domain) but does have
  a linked GitHub login -- the exact scenario this collector exists for.
- `history_page2.json` -- second (final) page, `hasNextPage: false`.
- `rate_limited_error.json` -- GitHub GraphQL's documented `RATE_LIMITED`
  error shape (`https://docs.github.com/graphql/overview/rate-limits-and-node-limits-for-the-graphql-api`).
