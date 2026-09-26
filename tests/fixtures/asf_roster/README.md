# ASF Roster Fixtures

## Sources

- `committee_info_cassandra.json`: Cassandra PMC roster with join dates
  - Source: https://whimsy.apache.org/public/committee-info.json
  - Downloaded: 2026-09-25
  - Contains: 49 PMC members with join dates (2010-02-17 to 2026-05-13)

- `public_ldap_projects_cassandra.json`: Full Cassandra project roster
  - Source: https://whimsy.apache.org/public/public_ldap_projects.json
  - Downloaded: 2026-09-25
  - Contains: 101 total members (49 owners/PMC + 52 non-PMC committers)
  - Real member/owner IDs replaced with synthetic names (user001-user101)
  - Structure unchanged: members and owners are lists of ID strings

## Structure Notes

- Members and owners are lists of ASF ID **strings**, not objects
- Example: members: ["user001", "user002", ...]
- This matches the exact structure from Whimsy's public API
