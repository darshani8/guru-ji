# The Guru Ji internet map: a guide for institution web administrators

The internet map keeps a graded list of an institution's official websites
and social accounts, so that staff, students and families can tell the real
accounts from look-alikes, and so that problems (a hacked site, a lapsed
domain, an impersonating account) are noticed early. This page explains what
the map reads from your sites and how you can confirm, correct or limit it.

## What the crawler does

* It identifies itself in the `User-Agent` header as
  `GuruJi-InstitutionIntelligence/1.0 (+<contact>)`, where the contact is a
  URL or email address of whoever runs the deployment. A production
  deployment cannot start the crawler without one, so write to that contact
  with any question or complaint about the crawler.
* It obeys `robots.txt`, re-read daily. Name it by its product token,
  without the version:

  ```
  User-agent: GuruJi-InstitutionIntelligence
  Disallow: /
  ```

  A group written for `GuruJi-InstitutionIntelligence/1.0` is matched the
  same way (the version is ignored). Without a group of its own, the `*`
  group applies.
* On an institution's own domain it reads the homepage and at most a few
  contact or about pages, about once a week. The scheduled reading sends
  the homepage's validators from the last clean reading (`If-None-Match` /
  `If-Modified-Since`), so an unchanged homepage costs a `304`; contact and
  about pages are always fetched in full, as is the homepage when a manager
  asks for a reading at once. The weekly ownership check (below) reads the
  homepage and `/.well-known/guruji.json` with conditional requests too.
* It does not log in anywhere, does not use anyone's session or
  credentials, does not solve CAPTCHAs, rotate proxies or disguise its user
  agent, and treats a `403`, `429` or login wall as "blocked", never as a
  reason to retry harder.
* It does not fetch pages on Facebook, Instagram, X, LinkedIn, YouTube,
  Threads, Reddit or Quora. Accounts there are known from links on your
  own site, from search-engine snippets, and from the platforms' public
  APIs where the operator has a key. The one exception is YouTube's public
  channel feed (`/feeds/videos.xml`), a machine endpoint read only when the
  operator enables feeds, and still subject to robots.txt.

## How accounts are graded

| Grade | Meaning |
|---|---|
| O | Confirmed by the institution itself (see below): a domain carrying its token, or an account named in that domain's verified `guruji.json`. |
| A | Linked as the site's own account (a header, navigation or footer link, a `rel="me"` link, a `sameAs` entry in the page's structured data, or a contact-page link naming the institution) on a healthy official page graded O or A; an official domain your administrators configured, while it is live and does not redirect elsewhere; a domain a regulator lists as yours (AICTE, UGC, NIRF, NAAC, NMC, VTU); or listed on a link hub or account page graded O, or a live subdomain of an O domain. |
| A-arch | Linked like that, but only in an archived copy, or before the site that linked it died, lapsed, was taken over or started redirecting. |
| B | Linked like that on an official page graded B; listed on a hub or account page graded A, or a live subdomain of an A domain; any other directory record; a reviewer's confirmation; two independent sources agreeing (search, directories, community records, hubs, backlinks, platform APIs, reviewers); or a configured domain that now redirects to another host. |
| C | One source only: a search snippet, a backlink, a community or platform-API record, a link from a page graded C or a hub graded B or lower, or an imported claim nobody has re-verified. |
| D | Refuted: rejected by a reviewer, a look-alike or an impersonator (only a reviewer's later confirmation lifts this), dead on two checks at least a day apart, or on a parked or hijacked domain. |

The most useful thing you can do is keep your official accounts linked from
your website's footer or header, or list them in `sameAs` or with
`rel="me"`: that alone makes them A.

## Confirming ownership (grade O)

Your institution's map managers can see an ownership token for each of your
own domains in the map (`GET /v1/intelligence/map/ownership`). Each domain
has its own token; publish it on that domain in any one of these ways:

1. A meta tag in the `<head>` of your homepage:

   ```html
   <meta name="guruji-verification" content="gj-…your token…">
   ```

2. A DNS TXT record on the domain:

   ```
   guruji-verification=gj-…your token…
   ```

3. A file at `https://<your-domain>/.well-known/guruji.json`, which can also
   name your accounts:

   ```json
   {
     "verification": "gj-…your token…",
     "accounts": [
       "https://www.instagram.com/your_account/",
       "https://www.youtube.com/@your_channel",
       "https://www.linkedin.com/school/your-college/"
     ]
   }
   ```

Any one method confirms the domain. The homepage and the file must be
served by the domain itself (`www.` is fine); a redirect to another host
proves nothing. Accounts listed in the file are graded O, unless the map
already knows the account as another organisation's, a look-alike's or a
person's. To withdraw one, remove it from the list; deleting the file or
breaking its token withdraws them all. The map checks weekly (managers can
also run the check at once, up to ten domains per request). Only a plain
answer withdraws anything: an outage, a block or a robots.txt refusal leaves
the confirmations as they were until the next check.

The token is tied to your institution and to that one domain on this
platform: a copy on any other host proves nothing, and it reveals nothing
else. Only domains your institution configured (or a reviewer confirmed) are
checked. The token is public, so the map trusts it only on a healthy site:
while a domain is reported compromised, hijacked, parked or redirecting it is
not checked, a hijack or a reviewer's rejection is lifted only by a reviewer,
and the accounts a lost domain listed stop being O. If a token may be in the
wrong hands (say a domain lapsed and someone else registered it), your
managers can issue new tokens (`POST /v1/intelligence/map/ownership/rotate`):
every earlier token stops proving anything, so publish the new one before the
next weekly check.

## When something is wrong

* **Your site is reported as compromised.** The map found hidden spam links
  (often injected through an outdated plugin) or spam pages indexed under
  your domain. Clean the site, update the CMS, themes and plugins, rotate
  admin and hosting passwords, and ask search engines to re-crawl. For
  Indian institutions, CERT-In's Directions of 28 April 2022 require covered
  organisations to report incidents such as website compromise within six
  hours (incident@cert-in.org.in); check whether they apply to you.
* **A domain is reported as expiring, parked or hijacked.** Renew it, or if
  it is no longer yours, remove every link to it, including from Wikipedia
  and old profiles, because whoever registers it next inherits that traffic.
* **An account is wrongly listed as yours, or a personal account appears.**
  Tell your institution's map managers. A reviewer can mark it a look-alike,
  an impersonator, or a person's own account; a personal account is removed
  from the map entirely and never added again.

## Opting out

Disallowing the crawler in `robots.txt` stops it reading your pages. Your
institution's managers can also stop the map (it is off unless enabled) or
suppress specific accounts or addresses (`POST /v1/intelligence/map/suppress`):
the map then keeps only a keyed fingerprint of them and never adds them again.
