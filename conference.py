#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime
from datetime import time

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.ext import ndb

from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import StringMessage
from models import BooleanMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import TeeShirtSize
from models import Session
from models import SessionForm
from models import SessionForms

from settings import WEB_CLIENT_ID
# from settings import ANDROID_CLIENT_ID
# from settings import IOS_CLIENT_ID
# from settings import ANDROID_AUDIENCE

from utils import getUserId

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
MEMCACHE_FEATURED_SPEAKER_KEY = "FEATURED_SPEAKER"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')
SPEAKER_TPL = ('See %s speaking at %s.')

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": ["Default", "Topic"],
}

SESSION_DEFAULTS = {
    "duration": 60,
    "typeOfSession": "Default type"
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage, websafeConferenceKey=messages.StringField(1),)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm, websafeConferenceKey=messages.StringField(1),)

SESSION_CREATE = endpoints.ResourceContainer(
    SessionForm, websafeConferenceKey=messages.StringField(1),)

SESSIONS_GET = endpoints.ResourceContainer(
    SessionForms, websafeConferenceKey=messages.StringField(1),)

SESSIONS_GETBYTYPE = endpoints.ResourceContainer(
    SessionForms, websafeConferenceKey=messages.StringField(1),
    type=messages.StringField(2),)

SESSIONS_GETBYSPEAKER = endpoints.ResourceContainer(
    SessionForms, speaker=messages.StringField(1),)

SESSION_WISHLIST = endpoints.ResourceContainer(
    SessionForms, websafeSessionKey=messages.StringField(1),)

CONF_GET_CITY = endpoints.ResourceContainer(
    message_types.VoidMessage, city=messages.StringField(1),)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1',
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        p_key = ndb.Key(Profile, user_id)
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email')
        return request


    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='GET', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in confs]
        )


    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in
                conferences])


# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key=p_key,
                displayName=user.nickname(),
                mainEmail=user.email(),
                teeShirtSize=str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")


    @staticmethod
    def _cacheFeaturedSpeaker(speaker, websafeConferenceKey):
        """When a new session is added to a conference, check the speaker.
        If there is more than one session by this speaker at this conference,
        add a new Memcache entry that features the speaker and session names.
        """
        # Need strong consistency here because our query needs to capture the put that brought us here
        ancestor_key = ndb.Key(urlsafe=websafeConferenceKey)
        sessions = Session.query(ancestor=ancestor_key)
        sessions = sessions.filter(Session.speaker == speaker)
        total = sessions.count()

        if total > 1 and speaker:       # speaker has more than 1 session, so he's Featured
            announcement = SPEAKER_TPL % (speaker, ', '.join(session.name for session in sessions))
            memcache.set(MEMCACHE_FEATURED_SPEAKER_KEY, announcement)
        else:
            announcement = ""
            memcache.delete(MEMCACHE_FEATURED_SPEAKER_KEY)
        return


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/featuredspeaker/get',
            http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Return Featured Speaker notification from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_FEATURED_SPEAKER_KEY) or "")

# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser()  # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser()  # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])
                    for conf in conferences])


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='filterPlayground',
            http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        q = Conference.query()
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)
        q = q.filter(Conference.city == "London")
        q = q.filter(Conference.topics == "Medical Innovations")
        q = q.filter(Conference.month == 6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )


# - - - Sessions - - - - - - - - - - - - - - - - - - - -

    # heavily borrowed from _createConference
    @endpoints.method(SESSION_CREATE, BooleanMessage, path='conference/{websafeConferenceKey}/session',
            http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new session - need user logged in and a valid Conference key"""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException('No conference found with that key')

        # user must be conference owner to create sessions
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException('Only the owner can create sessions.')

        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeSessionKey']        # how did that get in there?
        del data['websafeConferenceKey']        # how did that get in there?
        del data['conferenceName']              # in the form, but not the datastore class

        # add default values for those missing (both data model & outbound Message)
        for df in SESSION_DEFAULTS:
            if data[df] in (None, []):
                data[df] = SESSION_DEFAULTS[df]
                setattr(request, df, SESSION_DEFAULTS[df])

        # convert dates/times from strings to Date objects
        if data['date']:
            data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()
            if data['date'] < conf.startDate or data['date'] > conf.endDate:
                raise endpoints.BadRequestException("Session date not within conference's range")

        if data['startTime']:
            data['startTime'] = datetime.strptime(data['startTime'][:5], "%H:%M").time()
            print data['startTime']

        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        # data['websafeKey'] = request.websafeConferenceKey
        p_key = ndb.Key(Profile, user_id)
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        s_id = Session.allocate_ids(size=1, parent=c_key)[0]
        s_key = ndb.Key(Session, s_id, parent=c_key)
        data['key'] = s_key

        # create Session in datastore
        print data.items()
        Session(**data).put()
        # Check for speaker in more than one session
        # For the record, beyond getting practice with the task queue (which is great), I don't see why this
        # particular function would need or benefit from using the task queue.  Perhaps this Task 4 is a little
        # contrived.  Especially when you consider that task queues are good for things that can be delayed, 
        # and that what this particular task is doing requires immediate response from the datastore.
        taskqueue.add(params={'speaker': data['speaker'], 'confKey': request.websafeConferenceKey},
            url='/tasks/check_featured_speaker')
        return BooleanMessage(data=True)
        # return self._copySessionToForm(data, conf.name)

    # Heavily plagiarized from _copyConferenceToForm()
    def _copySessionToForm(self, session, confName):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(session, field.name):
                # convert date and times to string; just copy others
                if field.name == "date" or field.name == "startTime":
                    setattr(sf, field.name, str(getattr(session, field.name)))
                else:
                    setattr(sf, field.name, getattr(session, field.name))
            elif field.name == "websafeSessionKey":
                print "DEBUG: _copySessionToForm field.name = " + field.name
                print "DEBUG: _copySessionToForm urlsafe = " + session.key.urlsafe()
                setattr(sf, field.name, session.key.urlsafe())
        if confName:
            setattr(sf, 'conferenceName', confName)
        sf.check_initialized()
        return sf


    @endpoints.method(SESSIONS_GET, SessionForms, path='conference/{websafeConferenceKey}/sessions',
            http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Return sessions in a given conference"""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException('No conference found with that key')

        ancestor_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        sessions = Session.query(ancestor=ancestor_key)
        #sessions = Session.query(Session.websafeKey == request.websafeConferenceKey)
        sessions = sessions.order(Session.startTime)
        return SessionForms(
            items=[self._copySessionToForm(session, conf.name) for session in sessions]
        )


    @endpoints.method(SESSIONS_GETBYTYPE, SessionForms, path='conference/{websafeConferenceKey}/sessions/{type}',
            http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Return sessions in a given conference of a particular type"""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException('No conference found with that key')

        ancestor_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        sessions = Session.query(ancestor=ancestor_key)
        # sessions = Session.query(Session.websafeKey == request.websafeConferenceKey)
        sessions = sessions.filter(Session.typeOfSession == request.type)
        return SessionForms(
            items=[self._copySessionToForm(session, conf.name) for session in sessions]
        )


    @endpoints.method(SESSIONS_GETBYSPEAKER, SessionForms, path='conference/sessions/{speaker}',
            http_method='GET', name='getConferenceSessionsBySpeaker')
    def getConferenceSessionsBySpeaker(self, request):
        sessions = Session.query(Session.speaker == request.speaker)

        items = []
        for session in sessions:
            conf_key = session.key.parent()
            conference = conf_key.get()
            items.append(self._copySessionToForm(session, conference.name))
        return SessionForms(items=items)


# - - - Wishlist - - - - - - - - - - - - - - - - - - - -

    # addSessionToWishlist(SessionKey) -- adds the session to the user's list of sessions they are interested in attending
    #   You can decide if they can only add conference they have registered to attend or if the wishlist is open to all conferences.

    # heavily plagiarized from _conferenceRegistration
    @ndb.transactional(xg=True)
    def _wishlistToggle(self, request, add=True):
        """Add or Delete session from user wishlist"""
        retval = None
        prof = self._getProfileFromUser()  # get user Profile

        wssk = request.websafeSessionKey
        s_key = ndb.Key(urlsafe=wssk)
        session = s_key.get()
        if not session:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wssk)

        # add to WishList
        if add:
            # check if user already has it on their wishlist otherwise add
            if wssk in prof.sessionWishlistKeys:
                raise ConflictException(
                    "You already have that session in your wishlist")

            # add session to wishlist
            prof.sessionWishlistKeys.append(s_key)
            retval = True

        # remove from WishList
        else:
            # make sure the session is in their wishlist
            if s_key in prof.sessionWishlistKeys:
                prof.sessionWishlistKeys.remove(s_key)
                retval = True
            else:
                raise ConflictException(
                    "That session does not exist in your wishlist")

        # write things back to the datastore & return
        prof.put()
        return BooleanMessage(data=retval)


    @endpoints.method(SESSION_WISHLIST, BooleanMessage,
            path='wishlist/{websafeSessionKey}/add',
            http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Add Session to user's Wishlist"""
        return self._wishlistToggle(request, add=True)


    @endpoints.method(SESSION_WISHLIST, BooleanMessage,
            path='wishlist/{websafeSessionKey}/remove',
            http_method='POST', name='removeSessionFromWishlist')
    def removeSessionFromWishlist(self, request):
        """Remove Session from user's Wishlist"""
        return self._wishlistToggle(request, add=False)


    # getSessionsInWishlist() -- query for all the sessions in a conference that the user is interested in
    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='wishlist',
            http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Get Sessions from user's Wishlist"""
        prof = self._getProfileFromUser()  # get user Profile
        # wish_keys = [ndb.Key(urlsafe=wssk) for wssk in prof.sessionWishlistKeys]
        sessions = ndb.get_multi(prof.sessionWishlistKeys)

        items = []
        for session in sessions:
            conf_key = session.key.parent()
            conference = conf_key.get()
            items.append(self._copySessionToForm(session, conference.name))
        return SessionForms(items=items)

# - - - More Queries - - - - - - - - - - - - - - - - - - - -

    @endpoints.method(CONF_GET_CITY, ConferenceForms,
            path='conference/city/{city}',
            http_method='GET', name='getConferencesInCity')
    def getConferencesInCity(self, request):
        """Get Conferences in City"""
        conferences = Conference.query()
        conferences = conferences.filter(Conference.city == request.city)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in conferences]
        )


    @endpoints.method(SESSIONS_GET, SessionForms,
            path='conference/{websafeConferenceKey}/sessions/sleep-in',
            http_method='GET', name='getSessionsSleepIn')
    def getSessionsSleepIn(self, request):
        """Sessions you can still attend after a night of partying"""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        sessions = Session.query()
        sessions = sessions.order(Session.startTime)
        sessions = sessions.filter(Session.startTime >= time(10, 0, 0, 0))

        return SessionForms(
            items=[self._copySessionToForm(session, conf.name) for session in sessions])


    @endpoints.method(SESSIONS_GET, SessionForms,
            path='conference/{websafeConferenceKey}/sessions/daytime',
            http_method='GET', name='avoidLateAndWorkshops')
    def avoidLateAndWorkshops(self, request):
        """Get non-workshop sessions before 7 pm"""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        ancestor_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        sessions = Session.query(ancestor=ancestor_key)
        # sessions = Session.query(Session.websafeKey == request.websafeConferenceKey)
        sessions = sessions.order(Session.startTime)
        sessions = sessions.filter(Session.startTime < time(19, 0, 0, 0))
        # can't filter twice for inequalities, so do the second one programatically
        items = []
        for session in sessions:
            if session.typeOfSession != 'Workshop':
                items.append(self._copySessionToForm(session, conf.name))

        return SessionForms(items=items)


api = endpoints.api_server([ConferenceApi])  # register API
