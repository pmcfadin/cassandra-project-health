# JIRA Fixtures

Real recorded responses from Apache JIRA REST API for testing purposes.

## Files

- **search_with_reviewers.json**: First page (startAt=0, maxResults=5) of issues with reviewer field populated
  - URL: https://issues.apache.org/jira/rest/api/2/search?jql=project=CASSANDRA%20AND%20cf%5B12313420%5D%20is%20not%20EMPTY%20ORDER%20BY%20updated%20DESC&fields=summary,status,created,updated,resolutiondate,reporter,assignee,customfield_12313420,customfield_10022&maxResults=5
  - Recorded: 2026-09-25

- **search_unresolved.json**: Unresolved issues in CASSANDRA project
  - URL: https://issues.apache.org/jira/rest/api/2/search?jql=project=CASSANDRA%20AND%20resolution=Unresolved&fields=summary,status,created,updated,resolutiondate,reporter,assignee,customfield_12313420,customfield_10022&maxResults=5
  - Recorded: 2026-09-25

- **search_with_reviewers_page2.json**: Second page (startAt=5, maxResults=5) of issues with reviewer field
  - URL: https://issues.apache.org/jira/rest/api/2/search?jql=project=CASSANDRA%20AND%20cf%5B12313420%5D%20is%20not%20EMPTY%20ORDER%20BY%20updated%20DESC&fields=summary,status,created,updated,resolutiondate,reporter,assignee,customfield_12313420,customfield_10022&maxResults=5&startAt=5
  - Recorded: 2026-09-25

## Field Mappings

- **customfield_12313420**: Reviewer field (populated in CASSANDRA issues)
- **customfield_10022**: Additional field (may be null)

All responses are read-only anonymous queries from https://issues.apache.org
