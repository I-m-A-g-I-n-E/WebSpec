# Domain Verification

Domain verification proves that a service registrant controls the domains they claim.

## Why Verification Matters

Without verification, attackers could:

- Register `slack.gimme.tools` without owning Slack
- Intercept OAuth flows intended for legitimate services
- Phish users by impersonating known services
- Claim canonical_domains they don't control

---

## Verification Methods

### 1. DNS TXT Record (Recommended)

Add a TXT record to prove domain ownership:

```
_gimme-verify.acme.com  TXT  "gimme-tools-verify=abc123xyz"
```

**Pros:**

- Standard, well-understood method
- Works for any domain
- Doesn't require web server changes

**Cons:**

- DNS propagation delay (up to 48h)
- Requires DNS admin access

### 2. Well-Known File

Host a verification file:

```
https://acme.com/.well-known/gimme-tools-verify.txt

Contents: gimme-tools-verify=abc123xyz
```

**Pros:**

- Immediate verification
- Easy for web developers

**Cons:**

- Requires web server access
- File must be publicly accessible

### 3. Meta Tag

Add to homepage HTML:

```html
<meta name="gimme-tools-verify" content="abc123xyz">
```

**Pros:**

- No server config needed
- Just HTML change

**Cons:**

- Only works for domains with web pages
- Requires homepage modification

---

## Verification Flow

```
1. Service submits registration
              |
              v
2. gimme.tools generates unique verification token
   Token: "gimme-tools-verify=abc123xyz"
              |
              v
3. Service chooses verification method:
   - DNS TXT record
   - Well-known file
   - Meta tag
              |
              v
4. Service implements verification
              |
              v
5. Service calls /services/verify
              |
              v
6. gimme.tools checks all claimed domains:
   - Fetches DNS TXT records
   - Fetches well-known files
   - Scrapes meta tags
              |
              v
7a. All domains verified    -> Service activated
7b. Some domains fail       -> Partial activation + warning
7c. Primary domain fails    -> Registration rejected
```

---

## Multi-Domain Verification

Services often have multiple domains:

```yaml
canonical_domains:
  - acme.com           # Primary (required)
  - app.acme.com       # Subdomain
  - acme.io            # Alternative TLD
  - legacy-acme.com    # Legacy domain
```

### Verification Rules

| Domain Type | Requirement |
|---|---|
| Primary domain | Must verify |
| Subdomains of primary | Auto-verified if primary verified |
| Alternative domains | Must verify individually |
| Wildcard patterns | Must verify base domain |

### Example

```yaml
canonical_domains:
  - acme.com           # Verified via DNS
  - "*.acme.com"       # Auto-verified (subdomain of acme.com)
  - app.acme.io        # Verified via well-known file
  - oldacme.net        # Not verified (registration proceeds without this domain)
```

---

## Verification Token Security

```yaml
token:
  format: "gimme-tools-verify={random_32_bytes_hex}"
  lifetime: 7 days
  single_use: false (can re-verify)

security:
  - Tokens are unique per registration
  - Tokens cannot be reused across services
  - Tokens expire after 7 days
  - New token issued on re-registration
```

---

## Ongoing Verification

Domain ownership is re-verified periodically:

| Check | Frequency |
|---|---|
| DNS TXT still present | Weekly |
| Manifest still accessible | Daily |
| OAuth endpoints responding | Daily |
| SSL certificate valid | Daily |

### Verification Failure Handling

```
Day 1: Verification fails
  -> Warning email sent
  -> Service remains active

Day 3: Still failing
  -> Second warning
  -> Service flagged in admin console

Day 7: Still failing
  -> Service suspended
  -> Users see "Service temporarily unavailable"
  -> No new connections allowed

Day 14: Still failing
  -> Service deactivated
  -> Existing connections preserved but non-functional
  -> Subdomain released for re-registration
```

---

## API Reference

### Get Verification Status

```bash
GET https://api.gimme.tools/services/{service_id}/verification
Authorization: Bearer {admin_token}
```

Response:

```json
{
  "service_id": "acme",
  "status": "verified",
  "domains": [
    {
      "domain": "acme.com",
      "status": "verified",
      "method": "dns",
      "verified_at": "2024-12-14T10:00:00Z",
      "last_check": "2024-12-14T15:30:00Z"
    },
    {
      "domain": "acme.io",
      "status": "verified",
      "method": "well-known",
      "verified_at": "2024-12-14T10:05:00Z",
      "last_check": "2024-12-14T15:30:00Z"
    }
  ]
}
```

### Re-verify Domain

```bash
POST https://api.gimme.tools/services/{service_id}/verify
Authorization: Bearer {admin_token}

{
  "domains": ["acme.com"]  # Optional: specific domains, or omit for all
}
```

---

## Security Considerations

> **Important**: Verification proves domain control at a point in time. It does not guarantee ongoing trustworthiness.

Additional security measures:

- All services reviewed by gimme.tools team before activation
- User reports trigger immediate review
- Suspicious OAuth behavior triggers suspension
- Service reputation score based on user feedback
