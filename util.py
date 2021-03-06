# coding=utf-8
"""Misc utility constants and classes.
"""

import datetime
import re
import urllib
import urllib2
import urlparse

import requests
import webapp2

from activitystreams.oauth_dropins.webutil.models import StringIdModel
from activitystreams.oauth_dropins.webutil.util import *
from activitystreams import source
import apiclient
from appengine_config import HTTP_TIMEOUT, DEBUG
from oauth2client.client import AccessTokenRefreshError
from python_instagram.bind import InstagramAPIError

from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

# when running in dev_appserver, replace these domains in links with localhost
LOCALHOST_TEST_DOMAINS = frozenset(('kylewm.com', 'snarfed.org'))

EPOCH = datetime.datetime.utcfromtimestamp(0)
POLL_TASK_DATETIME_FORMAT = '%Y-%m-%d-%H-%M-%S'
FAILED_RESOLVE_URL_CACHE_TIME = 60 * 60 * 24  # a day

# rate limiting errors. twitter returns 429, instagram 503, google+ 403.
# TODO: facebook. it returns 200 and reports the error in the response.
# https://developers.facebook.com/docs/reference/ads-api/api-rate-limiting/
HTTP_RATE_LIMIT_CODES = frozenset(('403', '429', '503'))

# Known domains that don't support webmentions. Mainly just the silos.
WEBMENTION_BLACKLIST = {
  'amzn.com',
  'amazon.com',
  'brid.gy',
  'brid-gy.appspot.com',
  'example.com',
  'facebook.com',
  'm.facebook.com',
  'google.com',
  'instagr.am',
  'instagram.com',
  'ipv4.google.com',
  'plus.google.com',
  'twitter.com',
  # these come from the text of tweets. we also pull the expanded URL
  # from the tweet entities, so ignore these instead of resolving them.
  't.co',
  't',
  'twitpic.com',
  'youtube.com',
  'youtu.be',
  '', None,
  # these show up in the categories and tags sections of wordpress.com blog
  # posts. superfeedr doesn't filter them out of its 'content' field.
  'feeds.wordpress.com',
  'stats.wordpress.com',
  # temporary. tom's webmention handler is broken, and he knows about it.
  # TODO: remove once he's fixed it.
  'tommorris.org',
  # these are users' home pages that return 401 or 403
  'brandoncollins.org', # http://brid.gy/facebook/541818556, /twitter/wolbrandon
  'cssigniter.com',     # http://www.brid.gy/twitter/tsiger
  'localhero.biz',      # http://brid.gy/twitter/localherodotbiz
  }


def add_poll_task(source, **kwargs):
  """Adds a poll task for the given source entity.

  Tasks inserted from a backend (e.g. twitter_streaming) are sent to that
  backend by default, which doesn't work in the dev_appserver. Setting the
  target version to 'default' in queue.yaml doesn't work either, but setting it
  here does.

  Note the constant. The string 'default' works in dev_appserver, but routes to
  default.brid-gy.appspot.com in prod instead of www.brid.gy, which breaks SSL
  because appspot.com doesn't have a third-level wildcard cert.
  """
  last_polled_str = source.last_polled.strftime(POLL_TASK_DATETIME_FORMAT)
  task = taskqueue.add(queue_name='poll',
                       params={'source_key': source.key.urlsafe(),
                               'last_polled': last_polled_str},
                       **kwargs)
  logging.info('Added poll task with %s: %s', kwargs, task.name)


def add_propagate_task(entity, **kwargs):
  """Adds a propagate task for the given response entity.
  """
  task = taskqueue.add(queue_name='propagate',
                       params={'response_key': entity.key.urlsafe()},
                       target=taskqueue.DEFAULT_APP_VERSION,
                       **kwargs)
  logging.info('Added propagate task: %s', task.name)


def add_propagate_blogpost_task(entity, **kwargs):
  """Adds a propagate-blogpost task for the given response entity.
  """
  task = taskqueue.add(queue_name='propagate-blogpost',
                       params={'key': entity.key.urlsafe()},
                       target=taskqueue.DEFAULT_APP_VERSION,
                       **kwargs)
  logging.info('Added propagate-blogpost task: %s', task.name)


def email_me(**kwargs):
  """Thin wrapper around mail.send_mail() that handles errors."""
  try:
    mail.send_mail(sender='admin@brid-gy.appspotmail.com',
                   to='webmaster@brid.gy', **kwargs)
  except BaseException:
    logging.exception('Error sending notification email')


def follow_redirects(url):
  """Fetches a URL with HEAD, repeating if necessary to follow redirects.

  Caches resolved URLs in memcache. *Does not* raise an exception if any of the
  HTTP requests fail, just returns the failed response. If you care, be sure to
  check the returned response's status code!

  Args:
    url: string

  Returns:
    the requests.Response for the final request
  """
  cache_key = 'R ' + url
  resolved = memcache.get(cache_key)
  if resolved is not None:
    return resolved

  # can't use urllib2 since it uses GET on redirect requests, even if i specify
  # HEAD for the initial request.
  # http://stackoverflow.com/questions/9967632
  try:
    # default scheme to http
    parsed = urlparse.urlparse(url)
    if not parsed.scheme:
      url = 'http://' + url
    resolved = requests.head(url, allow_redirects=True, timeout=HTTP_TIMEOUT)
    cache_time = 0  # forever
  except AssertionError:
    raise
  except BaseException, e:
    logging.warning("Couldn't resolve URL %s : %s", url, e)
    resolved = requests.Response()
    resolved.url = url
    resolved.headers['content-type'] = 'text/html'
    resolved.status_code = 499  # not standard. i made this up.
    cache_time = FAILED_RESOLVE_URL_CACHE_TIME

  refresh = resolved.headers.get('refresh')
  if refresh:
    for part in refresh.split(';'):
      if part.strip().startswith('url='):
        return follow_redirects(part.strip()[4:])

  memcache.set(cache_key, resolved, time=cache_time)
  return resolved


def interpret_http_exception(exception):
  """Extracts the status code and response from different HTTP exception types.

  Args:
    exception: urllib2.HTTPError, InstagramAPIError, apiclient.errors.HttpError,
      requests.HTTPError, or oauth2client.client.AccessTokenRefreshError

  Returns: (string status code or None, string response body or None)
  """
  e = exception
  code = body = None

  if isinstance(e, urllib2.HTTPError):
    code = e.code
    try:
      body = e.read()
    except AttributeError:
      # no response body
      pass

  elif isinstance(e, requests.HTTPError):
    code = e.response.status_code
    body = e.response.text

  elif isinstance(e, apiclient.errors.HttpError):
    code = e.resp.status
    body = e.content

  elif isinstance(e, InstagramAPIError):
    if e.error_type == 'OAuthAccessTokenException':
      code = '401'
    else:
      code = e.status_code
    body = '%s: %s' % (e.error_type, e.error_message)

  elif isinstance(e, AccessTokenRefreshError) and str(e) == 'invalid_grant':
    code = '401'

  if code:
    code = str(code)
  if code or body:
    logging.warning('Error %s, response body: %s', code, body)

  return code, body


# Wrap webutil.util.tag_uri and hard-code the year to 2013.
#
# Needed because I originally generated tag URIs with the current year, which
# resulted in different URIs for the same objects when the year changed. :/
from activitystreams.oauth_dropins.webutil import util
_orig_tag_uri = tag_uri
util.tag_uri = lambda domain, name: _orig_tag_uri(domain, name, year=2013)


def get_webmention_target(url):
  """Resolves a URL and decides whether we should try to send it a webmention.

  Note that this ignores failed HTTP requests, ie the boolean in the returned
  tuple will be true! TODO: check callers and reconsider this.

  Returns: (string url, string pretty domain, boolean) tuple. The boolean is
    True if we should send a webmention, False otherwise, e.g. if it 's a bad
    URL, not text/html, or in the blacklist.
  """
  try:
    domain = domain_from_link(url)
  except BaseException, e:
    logging.warning('Dropping bad URL %s.', url)
    return (url, None, False)

  if domain and domain.startswith('mobile.'):
    domain = domain[len('mobile.'):]
  if domain in WEBMENTION_BLACKLIST:
    return (url, domain, False)

  url = clean_webmention_url(url)
  resolved = follow_redirects(url)
  if resolved.url != url:
    logging.debug('Resolved %s to %s', url, resolved.url)
    url = resolved.url
    domain = domain_from_link(url)

  is_html = resolved.headers.get('content-type', '').startswith('text/html')
  return (url, domain, is_html)


def clean_webmention_url(url):
  """Removes transient query params (e.g. utm_*) from a webmention target URL.

  The utm_* (Urchin Tracking Metrics?) params come from Google Analytics.
  https://support.google.com/analytics/answer/1033867

  Args:
    url: string

  Returns: string, the cleaned url
  """
  utm_params = set(('utm_campaign', 'utm_content', 'utm_medium', 'utm_source',
                    'utm_term'))
  parts = list(urlparse.urlparse(url))
  # parts[4] is the query
  params = [(name, value) for name, value in urlparse.parse_qsl(parts[4])
              if name not in utm_params]
  parts[4] = urllib.urlencode(params)
  return urlparse.urlunparse(parts)


def prune_activity(activity):
  """Returns an activity dict with just id, url, content, to, and object.

  If the object field exists, it's pruned down to the same fields. Any fields
  duplicated in both the activity and the object are removed from the object.

  Note that this only prunes the to field if it says the activity is public,
  since activitystreams.Source.is_public() defaults to saying an activity is
  public if the to field is missing. If that ever changes, we'll need to
  start preserving the to field here.

  Args:
    activity: ActivityStreams activity dict

  Returns: pruned activity dict
  """
  keep = ['id', 'url', 'content']
  if not source.Source.is_public(activity):
    keep += ['to']
  pruned = {f: activity.get(f) for f in keep}

  obj = activity.get('object')
  if obj:
    obj = pruned['object'] = prune_activity(obj)
    for k, v in obj.items():
      if pruned.get(k) == v:
        del obj[k]

  return trim_nulls(pruned)


def replace_test_domains_with_localhost(url):
  """Replace domains in LOCALHOST_TEST_DOMAINS with localhost for local
  testing when in DEBUG mode.

  Args:
    url: a string

  Returns: a string with certain well-known domains replaced by localhost
  """
  if url and DEBUG:
    for test_domain in LOCALHOST_TEST_DOMAINS:
      url = re.sub('https?://' + test_domain,
                   'http://localhost', url)
  return url


class Handler(webapp2.RequestHandler):
  """Includes misc request handler utilities.

  Attributes:
    messages: list of notification messages to be rendered in this page or
      wherever it redirects
  """

  def __init__(self, *args, **kwargs):
    super(Handler, self).__init__(*args, **kwargs)
    self.messages = set()

  def redirect(self, uri, **kwargs):
    """Adds self.messages to the fragment, separated by newlines.
    """
    parts = list(urlparse.urlparse(uri))
    if self.messages and not parts[5]:  # parts[5] is fragment
      parts[5] = '!' + urllib.quote('\n'.join(self.messages).encode('utf-8'))
    uri = urlparse.urlunparse(parts)
    super(Handler, self).redirect(uri, **kwargs)

  def maybe_add_or_delete_source(self, source_cls, auth_entity, state, **kwargs):
    """Adds or deletes a source if auth_entity is not None.

    Used in each source's oauth-dropins CallbackHandler finish() and get()
    methods, respectively.

    Args:
      source_cls: source class, e.g. Instagram
      auth_entity: ouath-dropins auth entity
      state: string, OAuth callback state parameter. For adds, this is just a
        feature ('listen' or 'publish') or empty. For deletes, it's
        [FEATURE]-[SOURCE KEY].
      kwargs: passed through to the source_cls constructor
    """
    if state is None:
      state = ''
    if state in ('', 'listen', 'publish', 'webmention'):  # this is an add/update
      if not auth_entity:
        if not self.messages:
          self.messages.add("OK, you're not signed up. Hope you reconsider!")
        self.redirect('/')
        return

      CachedPage.invalidate('/users')
      logging.info('%s.create_new with %s', source_cls.__class__.__name__,
                   (auth_entity.key, state, kwargs))
      source = source_cls.create_new(self, auth_entity=auth_entity,
                                     features=[state] if state else [],
                                     **kwargs)
      self.redirect(source.bridgy_url(self) if source else '/')
      return source

    else:  # this is a delete
      if auth_entity:
        self.redirect('/delete/finish?auth_entity=%s&state=%s' %
                      (auth_entity.key.urlsafe(), state))
      else:
        self.messages.add("OK, you're still signed up.")
        self.redirect(source.bridgy_url(self))

  def preprocess_source(self, source):
    """Prepares a source entity for rendering in the source.html template.

    - use id as name if name isn't provided
    - convert image URLs to https if we're serving over SSL

    Args:
      source: Source entity
    """
    if not source.name:
      source.name = source.key.string_id()
    if source.picture:
      source.picture = util.update_scheme(source.picture, self)
    return source


class CachedPage(StringIdModel):
  """Cached HTML for pages that changes rarely. Key id is path.

  Stored in the datastore since datastore entities in memcache (mostly
  Responses) are requested way more often, so it would get evicted
  out of memcache easily.

  Keys, useful for deleting from memcache:
  /: aglzfmJyaWQtZ3lyEQsSCkNhY2hlZFBhZ2UiAS8M
  /users: aglzfmJyaWQtZ3lyFgsSCkNhY2hlZFBhZ2UiBi91c2Vycww
  """
  html = ndb.TextProperty()
  expires = ndb.DateTimeProperty()

  @classmethod
  def load(cls, path):
    cached = CachedPage.get_by_id(path)
    if cached:
      if cached.expires and datetime.datetime.now() > cached.expires:
        logging.info('Deleting expired cached page for %s', path)
        cached.key.delete()
        return None
      else:
        logging.info('Found cached page for %s', path)
    return cached

  @classmethod
  def store(cls, path, html, expires=None):
    """path and html are strings, expires is a datetime.timedelta."""
    logging.info('Storing new page in cache for %s', path)
    if expires is not None:
      logging.info('  (expires in %s)', expires)
      expires = datetime.datetime.now() + expires
    CachedPage(id=path, html=html, expires=expires).put()

  @classmethod
  def invalidate(cls, path):
    logging.info('Deleting cached page for %s', path)
    CachedPage(id=path).key.delete()
