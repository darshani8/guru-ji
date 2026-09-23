# The Guru Ji internet map: a guide for institution web administrators

The internet map keeps a graded list of an institution's official websites
and social accounts, so that staff, students and families can tell the real
accounts from look-alikes, and so that problems (a hacked site, a lapsed
domain, an impersonating account) are noticed early. This page explains what
the map reads from your sites and how you can confirm, correct or limit it.

## What the crawler does

* It identifies itself in the `User-Agent` header as
  `GuruJi-InstitutionIntelligence/1.0`, followed by the operator's contact
  (a URL or email address set by the deployment).
* It obeys `robots.txt`. A `Disallow` for its user agent, or for `*`, is
  respected; robots.txt is re-read daily.
* On an institution's own domain it reads the homepage and at most a few
  contact or about pages, about once a week, using conditional requests
  (`If-None-Match` / `If-Modified-Since`), so an unchanged page costs a
  `304`.
* It does not log in anywhere, does not use anyone's session or
  credentials, does not solve CAPTCHAs, rotate proxies or disguise its user
  agent, and treats a `403`, `429` or login wall as "blocked", never as a
  reason to retry harder.
* It does not fetch pages on Facebook, Instagram, X, LinkedIn, YouTube,
  Threads, Reddit or Quora. Accounts there are known from links on your
  own site, from search-engine snippets, and from the platforms' public
  APIs where the operator has a key. YouTube's channel feeds
  (`/feeds/videos.xml`) are not read, because youtube.com's robots.txt
  disallows them; when the operator has a YouTube Data API key, a
  channel's last upload is read from its uploads playlist through the API.

## Other public sources it may use

Each of these is off until the operator enables it.

* **News feeds.** The public RSS feeds of a few English and Kannada news
  publishers, read once a day, are searched for items that name your
  institution. A mention never changes a grade or adds an account. One
  that mentions a court case or a controversy is shown to your map
  managers for review; the rest are only counted in the daily digest.
* **Look-alike domains.** Names one slip away from your own domain (a
  letter missing or doubled, another suffix such as `.com` or `.org`,
  look-alike Cyrillic letters) are resolved over DNS, and certificate
  transparency logs (SSLMate's Cert Spotter) are checked for your name
  under other suffixes. A name that is registered goes to your managers
  to look at; the map draws no conclusion from it.
* **Regulators' listings.** A listing counts as a regulator's only on these
  exact hosts: `facilities.aicte-india.org` and `www.aicte-india.org`
  (AICTE), `www.nirfindia.org` (NIRF), `www.ugc.gov.in` (UGC),
  `naac.gov.in` and `assessmentonline.naac.gov.in` (NAAC), `vtu.ac.in`
  (VTU), `rguhs.ac.in` and `www.rguhs.ac.in` (RGUHS), and `nmc.org.in` and
  `www.nmc.org.in` (NMC). Other subdomains of a regulator's domain do not
  count. The first time a regulator's listing names a domain that nothing
  else ties to your institution, a reviewer confirms it before it counts.
  PDF listings are not read.
* **The Wayback Machine.** For each of your own domains the map asks the
  Internet Archive once a month which hosts it has captured under that
  domain; a host the map does not know yet is visited once as a lead. For
  a domain that has lapsed or been taken over, archived copies of its
  homepage, contact and about pages show which accounts it linked then.

## OpenStreetMap

When the operator enables it, the map looks up each institution on
OpenStreetMap through the Nominatim API, within Nominatim's usage policy:

* requests carry the crawler's User-Agent with the operator's contact, and
  the connector cannot be enabled without one
  (`GURU_INTELLIGENCE_CRAWLER_CONTACT`);
* at least 15 seconds pass between requests, and each institution is
  looked up about once a month;
* the place is searched for once; after that the chosen object is re-read
  by its ID (`/lookup`), and searched for again only if it disappears or
  no longer carries the institution's name.

A campus's `website` and `contact:*` tags are recorded as a community
record: OpenStreetMap is edited by anyone, so its tags (like Wikidata's)
count as one source among others and never make an account official on
their own. Map data from OpenStreetMap is © OpenStreetMap contributors and
available under the Open Database License (ODbL); the map's export and
console attribute it wherever rows derived from it are shown. To correct
what OpenStreetMap says about your campus, edit it at
openstreetmap.org.

## How accounts are graded

| Grade | Meaning |
|---|---|
| O | Confirmed by the institution itself (see below). |
| A | Linked from the header, navigation or footer of a healthy official page, or an official domain your administrators configured. |
| A-arch | Linked like that, but only in an archived copy of a site that has since lapsed. |
| B | One step from an A source, a regulator's listing, a reviewer's confirmation, or two independent sources agreeing. |
| C | One source only, not yet confirmed. |
| D | Refuted: a look-alike, an impersonator, dead, or on a parked or hijacked domain. |

The most useful thing you can do is keep your official accounts linked from
your website's footer or header: that alone makes them A.

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

Any one method confirms the domain. Accounts listed in the file are graded
O. To withdraw one, remove it from the list; the map checks weekly (managers
can also run the check at once). The token is tied to your institution and
to that one domain on this platform: a copy on any other host proves
nothing, and it reveals nothing else. Only domains your institution
configured (or a reviewer confirmed) are checked.

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
