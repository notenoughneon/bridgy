"""Unit tests for twitter_streaming.py.
"""

import copy
import json
import mox
import threading

from google.appengine.ext import ndb
from tweepy import streaming

from activitystreams import twitter_test
from activitystreams.oauth_dropins import twitter as oauth_twitter
import models
import testutil
import twitter
import twitter_streaming
import util


class TwitterStreamingTest(testutil.ModelsTest):

  def setUp(self):
    super(TwitterStreamingTest, self).setUp()
    self.source = self.make_source('snarfed_org')
    self.listener = twitter_streaming.Listener(self.source)
    twitter_streaming.streams = {}
    twitter_streaming.UPDATE_STREAMS_PERIOD_S = .1

  def make_source(self, name):
    auth_entity = oauth_twitter.TwitterAuth(
      id=name, token_key='%s key' % name, token_secret='%s secret' % name,
      user_json='{}')
    auth_entity.put()
    return twitter.Twitter(id=name, auth_entity=auth_entity.key)

  def test_favorite(self):
    # missing data
    self.assertTrue(self.listener.on_data(json.dumps({'event': 'favorite'})))
    self.assertEqual(0, models.Response.query().count())

    # valid
    self.assertTrue(self.listener.on_data(json.dumps(twitter_test.FAVORITE_EVENT)))
    self.assertEqual(1, models.Response.query().count())
    resp = models.Response.query().get()
    self.assertEqual(twitter_test.LIKE_FROM_EVENT['id'], resp.key.string_id())
    self.assert_equals(twitter_test.LIKE_FROM_EVENT, json.loads(resp.response_json))

    activity = copy.deepcopy(twitter_test.ACTIVITY)
    self.assert_equals(activity, json.loads(resp.activities_json[0]))
    self.assert_equals(['http://first/link/'], resp.unsent)

  def test_retweet(self):
    retweet = copy.deepcopy(twitter_test.RETWEETS[0])
    retweet['retweeted_status'] = twitter_test.TWEET
    share = copy.deepcopy(twitter_test.SHARES[0])
    share['author']['id'] = 'tag:twitter.com,2013:alizz'
    activity = twitter_test.ACTIVITY
    share['object']['url'] = activity['url']

    self.assertTrue(self.listener.on_data(json.dumps(retweet)))
    self.assertEqual(1, models.Response.query().count())
    resp = models.Response.query().get()
    self.assertEqual(share['id'], resp.key.string_id())
    self.assert_equals(share, json.loads(resp.response_json))
    self.assert_equals(activity, json.loads(resp.activities_json[0]))
    self.assert_equals(['http://first/link/'], resp.unsent)

  # disabled for now. see comment in twitter_streaming.py for details.
  # def test_reply(self):
  #   tw_reply = copy.deepcopy(twitter_test.REPLIES_TO_SNARFED['statuses'][0])
  #   as_reply = twitter_test.ACTIVITY_WITH_REPLIES['object']['replies']['items'][0]
  #   as_reply['author']['id'] = 'tag:twitter.com,2013:alice'
  #   self.expect_urlopen(
  #     'https://api.twitter.com/1.1/statuses/show.json?id=100&include_entities=true',
  #     json.dumps(twitter_test.TWEET))
  #   self.mox.ReplayAll()

  #   self.assertTrue(self.listener.on_data(json.dumps(tw_reply)))
  #   self.assertEqual(1, models.Response.query().count())
  #   resp = models.Response.query().get()
  #   self.assertEqual(as_reply['id'], resp.key.string_id())
  #   self.assert_equals(as_reply, json.loads(resp.response_json))
  #   self.assert_equals(twitter_test.ACTIVITY, json.loads(resp.activities_json[0]))
  #   self.assert_equals(['http://first/link/'], resp.unsent)

  def test_unhandled_event(self):
    self.assertTrue(self.listener.on_data(json.dumps({'event': 'foo'})))
    self.assertEqual(0, models.Response.query().count())

  def test_not_json(self):
    # bad json raises an exception inside on_data()
    self.assertTrue(self.listener.on_data('not json'))

  def test_update_streams_shutdown_exception(self):
    orig_uso = twitter_streaming.update_streams_once
    def new_uso():
      raise twitter_streaming.ShutdownException()

    try:
      twitter_streaming.update_streams_once = new_uso
      twitter_streaming.update_streams()
      self.assertIsNone(twitter_streaming.update_thread)
    finally:
      twitter_streaming.update_streams_once = orig_uso

  def test_update_streams(self):
    sources = {name: self.make_source(name) for name in
               ('existing', 'new', 'disabled', 'error', 'deleted', 'stopped')}
    sources['disabled'].status = 'disabled'
    sources['error'].status = 'error'
    for source in sources.values():
      source.put()

    for name in 'existing', 'error', 'disabled', 'deleted', 'stopped':
      stream = self.mox.CreateMock(streaming.Stream)
      stream.running = (name != 'stopped')
      twitter_streaming.streams[sources[name].key] = stream

    # expect connects and disconnects
    self.mox.StubOutClassWithMocks(streaming, 'Stream')
    for name in 'stopped', 'new':
      streaming.Stream(mox.IgnoreArg(), mox.IgnoreArg(), secure=True
                       ).userstream(async=True)
    for name in 'disabled', 'deleted':
      twitter_streaming.streams[sources[name].key].disconnect()

    self.mox.ReplayAll()

    sources['deleted'].key.delete()
    twitter_streaming.update_streams_once()

    self.assert_equals([sources['existing'].key,
                        sources['error'].key,
                        sources['new'].key,
                        sources['stopped'].key,
                        ],
                       twitter_streaming.streams.keys())

  # def test_stop_update_streams_once_exception(self):
  #   self.mox.StubOutWithMock(twitter_streaming, 'update_streams_once')

  #   # first call raises exception
  #   twitter_streaming.update_streams_once().AndRaise(Exception('foo'))

  #   # second call stops thread
  #   def stop_update_thread():
  #     twitter_streaming.update_thread = None
  #   twitter_streaming.update_streams_once().WithSideEffects(stop_update_thread)

  #   self.mox.ReplayAll()
  #   twitter_streaming.update_streams()
