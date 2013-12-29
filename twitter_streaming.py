"""Twitter Streaming API client for receiving and handling favorites.

Favorites are reported via 'favorite' events:
https://dev.twitter.com/docs/streaming-apis/messages#Events_event

The Twitter Streaming API uses long-lived HTTP requests, so this is an App
Engine backend instead of a normal request handler.
https://developers.google.com/appengine/docs/python/backends/

It also (automatically) uses App Engine's Sockets API:
https://developers.google.com/appengine/docs/python/sockets/

An alternative to Twitter's Streaming API would be to scrape the HTML, e.g.:
https://twitter.com/i/activity/favorited_popup?id=415371781264781312

I originally used the requests library, which is great, but tweepy handles more
logic that's specific to Twitter's Streaming API, e.g. backoff for HTTP 420 rate
limiting.

Also, SSL with App Engine's sockets API isn't fully supported in dev_appserver.
urllib3.util.ssl_wrap_socket() makes ssl.py raise 'TypeError: must be
_socket.socket, not socket'. To work around that:

* add '_ssl' and '_socket' to _WHITE_LIST_C_MODULES in
  SDK/google/appengine/tools/devappserver2/python/sandbox.py
* replace SDK/google/appengine/dist27/socket.py with /usr/lib/python2.7/socket.py

Background:
http://stackoverflow.com/a/16937668/186123
http://code.google.com/p/googleappengine/issues/detail?id=9246
https://developers.google.com/appengine/docs/python/sockets/ssl_support
"""

__author__ = ['Ryan Barrett <bridgy@ryanb.org>']

import json
import logging
import threading
import time

from activitystreams.oauth_dropins import twitter as oauth_twitter
import appengine_config
import models
import tasks
from tweepy import streaming
import twitter
import util

import webapp2

USER_STREAM_URL = 'https://userstream.twitter.com/1.1/user.json?with=user'
# How often to check for new/deleted sources, in seconds.
POLL_FREQUENCY_S = 5 * 60

# global. maps twitter.Twitter key to tweepy.streaming.Stream
streams = {}


class FavoriteListener(streaming.StreamListener):
  """A per-user streaming API connection that saves favorites as Responses.

  I'd love to use non-blocking I/O on the HTTP connections instead of thread per
  connection, but tweepy's API is at a way higher level: its only options are
  blocking or threads. Same with requests, and I think even httplib. Ah well.
  It'll be fine as long as brid.gy doesn't have a ton of users.
  """

  def __init__(self, source):
    """Args: source: twitter.Twitter
    """
    super(FavoriteListener, self).__init__()
    self.source = source

  def on_connect(self):
    logging.info('Connected! (%s)', self.source.key().name())

  def on_data(self, raw_data):
    try:
      data = json.loads(raw_data)
      if data.get('event') != 'favorite':
        # logging.debug('Discarding non-favorite message: %s', raw_data)
        return True

      like = self.source.as_source.streaming_event_to_object(data)
      if not like:
        logging.debug('Discarding malformed favorite event: %s', raw_data)
        return True

      tweet = data.get('target_object')
      activity = self.source.as_source.tweet_to_activity(tweet)
      targets = tasks.get_webmention_targets(activity)
      models.Response(key_name=like['id'],
                      source=self.source,
                      activity_json=json.dumps(activity),
                      response_json=json.dumps(like),
                      unsent=list(targets),
                      ).get_or_save()
      # TODO: flush logs to generate a log per favorite event, so we can link
      # to each one from the dashboard.
      # https://developers.google.com/appengine/docs/python/backends/#Python_Periodic_logging
    except:
      logging.exception('Error processing message: %s', raw_data)

    return True

class Start(webapp2.RequestHandler):
  def get(self):
    global streams

    while True:
      query = twitter.Twitter.all().filter('status !=', 'disabled')
      sources = {t.key(): t for t in query}
      stream_keys = set(streams.keys())
      source_keys = set(sources.keys())

      # Connect to new accounts
      for key in source_keys - stream_keys:
        logging.info('Connecting to %s %s', key.name(), key)
        source = sources[key]
        auth = oauth_twitter.TwitterAuth.tweepy_auth(
          *source.auth_entity.access_token())
        streams[key] = streaming.Stream(auth, FavoriteListener(source))
        streams[key].userstream(async=True)  # async=True starts a thread

      # Disconnect from deleted or disabled accounts
      # TODO: if access is revoked on Twitter's end, the request will disconnect.
      # handle that.
      # https://dev.twitter.com/docs/streaming-apis/messages#Events_event
      # for key in source_keys - stream_keys:
      #   streams[key].stop()
      #   del streams[key]

      time.sleep(POLL_FREQUENCY_S)


class Stop(webapp2.RequestHandler):
  def get(self):
    logging.info('Stopping.')
    # TODO


application = webapp2.WSGIApplication([
    ('/_ah/start', Start),
    ('/_ah/stop', Stop),
    # duplicate this task queue handler here since App Engine tries to run tasks
    # inline first, in this backend, when they're inserted.
    ('/_ah/queue/propagate', tasks.Propagate),
    ], debug=appengine_config.DEBUG)