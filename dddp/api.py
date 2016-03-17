"""Django DDP API, Collections, Cursors and Publications."""
from __future__ import absolute_import, unicode_literals, print_function

# standard library
import collections
from copy import deepcopy
import inspect
import uuid
import itertools
# requirements
from django.conf import settings
import django.contrib.postgres.fields
from django.db import connections, router, transaction
from django.db import models
from django.db.models import Q
try:
    # pylint: disable=E0611
    from django.db.models.expressions import ExpressionNode
except ImportError:
    from django.db.models import Expression as ExpressionNode
from django.utils.encoding import force_text
from django.utils.module_loading import import_string
from django.db import DatabaseError
from django.db.models import signals
from django.utils.functional import cached_property
import ejson
import six

# django-ddp
from dddp import AlreadyRegistered, this, ADDED, CHANGED, REMOVED, MeteorError
from dddp.models import (
    AleaIdField, Connection, Subscription, SubscriptionCollection,get_meteor_id, get_meteor_ids,
)
from functools import wraps


API_ENDPOINT_DECORATORS = [
    import_string(dotted_path)
    for dotted_path
    in getattr(settings, 'DDP_API_ENDPOINT_DECORATORS', [])
]

XMIN = {'select': {'xmin': "'xmin'"}}

# Only do this if < django1.9?

if django.VERSION < (1, 9):
    from django.db.models import aggregates

    # pylint: disable=W0223
    class ArrayAgg(aggregates.Aggregate):
        function = 'ARRAY_AGG'
else:
    from django.contrib.postgres.aggregates import ArrayAgg


def api_endpoint(path_or_func=None, decorate=True):
    """
    Decorator to mark a method as an API endpoint for later registration.

    Args:
        path_or_func: either the function to be decorated or its API path.
        decorate (bool): Apply API_ENDPOINT_DECORATORS if True (default).

    Returns:
        Callable: Decorated function (with optionally applied decorators).

    Examples:

        >>> from dddp.api import APIMixin, api_endpoint
        >>> class Counter(APIMixin):
        ...     value = 0
        ...
        ...     # default API path matches function name 'increment'.
        ...     @api_endpoint
        ...     def increment(self, amount):
        ...         '''Increment counter value by `amount`.'''
        ...         self.value += amount
        ...         return self.value
        ...
        ...     # excplicitly set API path to 'Decrement'.
        ...     @api_endpoint('Decrement')
        ...     def decrement(self, amount):
        ...         '''Decrement counter value by `amount`.'''
        ...         self.value -= amount
        ...         return self.value

    """
    def maybe_decorated(func):
        """Apply API_ENDPOINT_DECORATORS to func."""
        if decorate:
            for decorator in API_ENDPOINT_DECORATORS:
                func = decorator()(func)
        return func
    if callable(path_or_func):
        path_or_func.api_path = path_or_func.__name__
        return maybe_decorated(path_or_func)
    else:
        def _api_endpoint(func):
            """Decorator inner."""
            if path_or_func is None:
                func.api_path = func.__name__
            else:
                func.api_path = path_or_func
            return maybe_decorated(func)
        return _api_endpoint


def api_endpoints(obj):
    """Iterator over all API endpoint names and callbacks."""
    for name in dir(obj):
        attr = getattr(obj, name)
        api_path = getattr(attr, 'api_path', None)
        if api_path:
            yield (
                '%s%s' % (obj.api_path_prefix, api_path),
                attr,
            )
    for api_provider in obj.api_providers:
        for api_path, attr in api_endpoints(api_provider):
            yield (api_path, attr)


class APIMeta(type):

    """DDP API metaclass."""

    def __new__(cls, name, bases, attrs):
        """Create a new APIMixin class."""
        attrs['name'] = attrs.pop('name', None) or name
        name_format = attrs.get('name_format', None)
        if name_format:
            attrs['name'] = name_format.format(**attrs)
        api_path_prefix_format = attrs.get('api_path_prefix_format', None)
        if attrs.get('api_path_prefix', None) is not None:
            pass
        elif api_path_prefix_format is not None:
            attrs['api_path_prefix'] = api_path_prefix_format.format(**attrs)
        return super(APIMeta, cls).__new__(cls, name, bases, attrs)


class APIMixin(object):

    """Mixin to support finding API endpoints for class instances."""

    api_path_prefix_format = None

    api_providers = []
    api_path_prefix = '/'
    _api_path_cache = None

    def api_path_map(self):
        """Cached dict of api_path: func."""
        if self._api_path_cache is None:
            self._api_path_cache = {
                api_path: func
                for api_path, func
                in api_endpoints(self)
            }
        return self._api_path_cache

    def clear_api_path_map_cache(self):
        """Clear out cache for api_path_map."""
        self._api_path_cache = None
        for api_provider in self.api_providers:
            if six.get_method_self(
                api_provider.clear_api_path_map_cache,
            ) is not None:
                api_provider.clear_api_path_map_cache()

    def api_endpoint(self, api_path):
        """Return API endpoint for given api_path."""
        return self.api_path_map()[api_path]

    def ready(self):
        """Initialisation (setup lookups and signal handlers)."""
        pass


def model_name(model):
    """Return model name given model class."""
    # Django supports model._meta -> pylint: disable=W0212
    return force_text(model._meta)


COLLECTION_PATH_FORMAT = '/{name}/'


class CollectionMeta(APIMeta):

    """DDP Collection metaclass."""

    def __new__(mcs, name, bases, attrs):
        """Create a new Collection class."""
        attrs.update(
            api_path_prefix_format=COLLECTION_PATH_FORMAT,
        )
        model = attrs.get('model', None)
        if attrs.get('name', None) is None and model is not None:
            attrs.update(
                name=model_name(model),
            )
        return super(CollectionMeta, mcs).__new__(mcs, name, bases, attrs)


@six.add_metaclass(CollectionMeta)
class Collection(APIMixin):

    """DDP Model Collection."""

    name = None
    model = None
    qs_filter = None
    order_by = None
    user_rel = None
    always_allow_superusers = True
    sync_fields=None
    no_sync_fields=None

    @cached_property
    def _to_sync_fields(self):
        if self.sync_fields:
            return set(self.sync_fields)

        no_sync_fields=self.no_sync_fields or []

        return {
            field.name
            for field in itertools.chain(self.model._meta.local_fields,self.model._meta.local_many_to_many)
            if field.name not in no_sync_fields
        }

    def get_queryset(self, base_qs=None):
        """Return a filtered, ordered queryset for this collection."""
        qs = self.model.objects.all() if base_qs is None else base_qs
        # enforce ordering so later use of distinct() works as expected.
        if not qs.query.order_by:
            if self.order_by is None:
                qs = qs.order_by('pk')
            else:
                qs = qs.order_by(*self.order_by)
        if self.qs_filter:
            qs = qs.filter(self.qs_filter)
        return qs

    queryset = property(get_queryset)

    @cached_property
    def aid_field(self):
        meta=self.model._meta
        if isinstance(meta.pk, AleaIdField):
            return meta.pk
        alea_unique_fields = [
            field
            for field in meta.local_fields
            if isinstance(field, AleaIdField) and field.unique
        ]
        if len(alea_unique_fields) == 1:
            # found an AleaIdField with unique=True, assume it's got the value.
            return alea_unique_fields[0]
        return None

    @cached_property
    def user_rels(self):
        if not self.user_rel:
            return None
        return [self.user_rel] if isinstance(self.user_rel, basestring) else self.user_rel

    @cached_property
    def reversed_user_rel(self):
        if not self.user_rels:
            return None
        return [self.get_reversed_user_rel(user_rel) for user_rel in self.user_rels]

    def get_reversed_user_rel(self,user_rel):
        fields=user_rel.split("__")
        model=self.model
        reverse_name=[]
        for field_name in fields:
            if field_name=="pk":
                field=model._meta.pk
            else:
                field=model._meta.get_field(field_name)
                model=field.related_model
                if isinstance(field, (models.ManyToManyField,models.ForeignKey,models.OneToOneField)):
                    related_name=field.related_query_name()
                elif isinstance(field,(models.ManyToManyRel,models.ManyToOneRel,models.OneToOneRel)):
                    related_name=field.field.name
                reverse_name.append(related_name)
        return "__".join(reversed(reverse_name))

    @property
    def user_model(self):
        """Cached property getter around `get_user_model`."""
        from django.contrib.auth import get_user_model
        val = self.__dict__['user_model'] = get_user_model()
        return val

    def objects_for_user(self, user, qs=None, xmin__lte=None):
        """Find objects in queryset related to specified user."""
        qs = self.get_queryset(qs)
        user_rels = self.user_rel
        if user_rels:
            if user is None:
                return qs.none()  # no user but we need one: return no objects.
            if isinstance(user_rels, basestring):
                user_rels = [user_rels]
            user_filter = None
            # Django supports model._meta -> pylint: disable=W0212
            meta = self.model._meta
            for user_rel in user_rels:
                name, rel = (user_rel.split('__', 1) + [None])[:2]
                field = meta.pk if name == 'pk' else meta.get_field(name)
                # generate `filter_obj` (instance of django.db.models.Q)
                # if field.related_model is not None:
                # user_rel spans a join - ensure efficient SQL is generated
                # such as `...WHERE foo_id IN (SELECT foo.id FROM ...)`
                # rather than creating an explosion of INNER JOINS.
                to_field_name=field.to_fields[0] if isinstance(field, models.ForeignKey) else "pk"
                to_field_name=to_field_name or "pk"
                # import ipdb; ipdb.set_trace()
                filter_obj = Q(**({
                    '%s__in' % name: field.related_model.objects.filter(
                        **({rel: user})
                    ).values(to_field_name)
                } if rel else {
                    name:getattr(user, to_field_name)
                }))
                # else:
                #     # user rel is a local field -> no joins to avoid.
                #     import ipdb; ipdb.set_trace()
                #     filter_obj = Q(**{str(user_rel): user})
                # merge `filter_obj` into `user_filter`
                if user_filter is None:
                    user_filter = filter_obj
                else:
                    user_filter |= filter_obj
            qs = qs.filter(user_filter).distinct()
        if xmin__lte is not None:
            qs = qs.extra(
                where=["'xmin' <= %s"],
                params=[xmin__lte],
            )
        return qs

    def user_ids_for_object(self, obj):
        """Find user IDs related to object/pk in queryset."""
        qs = self.queryset
        if self.user_rel:
            user_ids = set()
            if obj.pk is None:
                return user_ids  # nobody can see objects that don't exist
            user_rels = self.user_rel
            if isinstance(user_rels, basestring):
                user_rels = [user_rels]
            user_rel_map = {
                '_user_rel_%d' % index: ArrayAgg(user_rel)
                for index, user_rel
                in enumerate(user_rels)
            }

            if self.always_allow_superusers:
                user_ids.update(
                    self.user_model.objects.filter(
                        is_superuser=True, is_active=True,
                    ).values_list('pk', flat=True)
                )

            for rel_user_ids in qs.filter(
                    pk=obj.pk,
            ).annotate(
                **user_rel_map
            ).values_list(
                *user_rel_map.keys()
            ).get():
                user_ids.update(rel_user_ids)
            user_ids.difference_update([None])
            return user_ids
        else:
            return None

    def field_schema(self):
        """Generate schema for consumption by clients."""
        type_map = {
            'AutoField': 'String',
            'BooleanField': 'Boolean',
            'CharField': 'String',
            'DateTimeField': 'Date',
            'DecimalField': 'Number',
            'FloatField': 'Number',
            'ForeignKey': 'String',
            'PositiveIntegerField': 'Number',
            'TextField': 'String',
        }
        db_type_map = {
            'serial': 'Number',
            'text': 'String',
            'boolean': 'Boolean',
            'integer': 'Number',
        }
        # Django supports model._meta -> pylint: disable=W0212
        meta = self.model._meta
        connection = connections[router.db_for_read(self.model.objects.none())]
        for field in meta.local_fields:
            int_type = field.get_internal_type()
            schema = {
                'type': (
                    type_map.get(int_type, None)
                ) or (
                    db_type_map.get(field.db_type(connection), 'String')
                )
            }

            rel = getattr(field, 'rel', None)
            if rel:
                schema['type'] = 'String'
                schema['relation'] = {
                    'name': field.name,
                    'collection': model_name(rel.to),
                }

            choices = getattr(field, 'choices', None)
            if choices:
                schema['allowedValues'] = [val for val, _ in choices]
                schema['autoform'] = {
                    'options': [
                        {'label': desc, 'value': val}
                        for val, desc in choices
                    ],
                }

            blank = getattr(field, 'blank', None)
            if blank:
                schema['optional'] = True

            formfield = field.formfield()
            if formfield:
                schema['label'] = force_text(formfield.label)

            max_length = getattr(field, 'max_length', None)
            if max_length is not None:
                schema['max'] = max_length

            if int_type == 'PositiveIntegerField':
                schema['min'] = 0
            if int_type in ('DecimalField', 'FloatField'):
                schema['decimal'] = True
            yield field.column, schema

        for field in meta.local_many_to_many:
            schema = {
                'type': '[String]',
                'relation': {
                    'name': field.name,
                    'collection': model_name(field.rel.to),
                },
            }

            blank = getattr(field, 'blank', None)
            if blank:
                schema['optional'] = True

            formfield = field.formfield()
            if formfield:
                schema['label'] = force_text(formfield.label)

            yield '%s_ids' % field.column, schema

    @api_endpoint
    def schema(self):
        """Return a representation of the schema for this collection."""
        return {
            name: schema
            for name, schema
            in self.field_schema()
        }

    def serialize(self, obj, meteor_ids,no_sync_fields=None):
        """Generate a DDP msg for obj with specified msg type."""
        no_sync_fields=no_sync_fields or []
        # check for F expressions
        exps = [
            name for name, val in vars(obj).items()
            if isinstance(val, ExpressionNode)
        ]
        if exps:
            # clone/update obj with values but only for the expression fields
            obj = deepcopy(obj)
            for name, val in self.model.objects.values(*exps).get(
                    pk=obj.pk,
            ).items():
                setattr(obj, name, val)

        # run serialization now all fields are "concrete" (not F expressions)
        data = this.serializer.serialize([obj])[0]
        fields = data['fields']
        del data['pk'], data['model']
        # Django supports model._meta -> pylint: disable=W0212
        meta = self.model._meta
        for field in meta.local_fields:
            if field.name not in self._to_sync_fields or field.name in no_sync_fields:
                continue
            rel = getattr(field, 'rel', None)
            if rel:
                if rel.model in API._model_cols:
                    # use field value which should set by select_related()
                    to_field_name=field.to_fields[0]
                    if to_field_name and (to_field_name=="aid" or isinstance(field.related_model._meta.get_field(to_field_name),AleaIdField)):
                        fields[field.column]=getattr(obj, field.attname)
                    else:
                        fields[field.column] = get_meteor_id(
                            field.related_model,
                            getattr(obj, field.attname),
                        )
                else:
                    fields[field.column]=getattr(obj,field.attname)
                fields.pop(field.name)
            elif isinstance(field, django.contrib.postgres.fields.ArrayField):
                fields[field.name] = field.to_python(fields.pop(field.name))
            elif (
                isinstance(field, AleaIdField)
            ) and (
                not field.null
            ) and (
                field.name == 'aid'
            ) and not field.primary_key:
                # This will be sent as the `id`, don't send it in `fields`.
                fields.pop(field.name)
        for field in meta.local_many_to_many:
            if field.name not in self._to_sync_fields or field.name in no_sync_fields:
                continue
            qs=getattr(obj,field.name).all()
            try:
                if field.rel.model in API._model_cols:
                    field_name=API._model_aid[qs.model].name
                else:
                    field_name="pk"
                data_=list(qs.values_list(field_name,flat=True))
                if data_ and isinstance(data_[0],uuid.UUID):
                    data_=[str(item) for item in data_]
            except KeyError:
                data_ = get_meteor_ids(
                    field.rel.to, qs.values_list("pk",flat=True),
                ).values()
            fields['%s_ids' % field.name]=data_
        return data

    def obj_change_as_msg(self, obj, msg, meteor_ids=None):
        """Return DDP change message of specified type (msg) for obj."""
        if meteor_ids is None:
            meteor_ids = {}
        try:
            meteor_id = meteor_ids[str(obj.pk)]
        except KeyError:
            meteor_id = None
        if meteor_id is None:
            meteor_ids[str(obj.pk)] = meteor_id = get_meteor_id(obj)
        assert meteor_id is not None
        if msg == REMOVED:
            data = {}  # `removed` only needs ID (added below)
        elif msg in (ADDED, CHANGED):
            data = self.serialize(obj, meteor_ids)
        else:
            raise ValueError('Invalid message type: %r' % msg)

        data.update(msg=msg, collection=self.name, id=meteor_id)
        return data


class PublicationMeta(APIMeta):

    """DDP Publication metaclass."""

    def __new__(mcs, name, bases, attrs):
        """Create a new Publication class."""
        attrs.update(
            api_path_prefix_format='publication/{name}/',
        )
        return super(PublicationMeta, mcs).__new__(mcs, name, bases, attrs)


@six.add_metaclass(PublicationMeta)
class Publication(APIMixin):

    """DDP Publication (a set of queries)."""

    name = None
    queries = None

    def user_queries(self, user, *params):
        """Return queries for this publication as seen by `user`."""
        try:
            get_queries = self.get_queries
        except AttributeError:
            # statically defined queries
            if self.queries is None:
                raise NotImplementedError(
                    'Must set either queries or implement get_queries method.',
                )
            if len(params)>1:  # Env object
                raise NotImplementedError(
                    'Publication params not implemented on %r publication.' % (
                        self.name,
                    ),
                )
            return self.queries[:]

        if user is False:
            # no need to play with `this.user_id` or `this.user_ddp_id`.
            return get_queries(*params)

        # stash the old user details
        old_user_id = this.user_id
        old_user = this.user
        old_user_ddp_id = this.user_ddp_id
        # apply the desired user details
        this.user_id = None if user is None else user.pk
        this.user_ddp_id = None if user is None else get_meteor_id(user)
        this.user=user
        try:
            return get_queries(*params)
        finally:
            # restore the old user details
            this.user_id = old_user_id
            this.user_ddp_id = old_user_ddp_id
            this.user=old_user

    @api_endpoint
    def collections(self, *params):
        """Return list of collections for this publication."""
        return sorted(
            set(
                hasattr(qs, 'model') and model_name(qs.model) or qs[1]
                for qs
                in self.get_queries(False, *params)
            )
        )


@six.add_metaclass(APIMeta)
class DDP(APIMixin):

    """Django DDP API."""

    pgworker = None
    _in_migration = False

    def __init__(self):
        """DDP API init."""
        self._registry = {}
        self._model_cols=collections.defaultdict(list)
        self._model_aid={}
        self._ddp_subscribers = {}

    def get_collection(self, model):
        """Return collection instance for given model."""
        name = model_name(model)
        return self.get_col_by_name(name)

    def get_col_by_name(self, name):
        """Return collection instance for given name."""
        return self._registry[COLLECTION_PATH_FORMAT.format(name=name)]

    def get_pub_by_name(self, name):
        """Return publication instance for given name."""
        path = Publication.api_path_prefix_format.format(name=name)
        return self._registry[path]

    @property
    def api_providers(self):
        """Return an iterable of API providers."""
        return self._registry.values()

    def qs_and_collection(self, qs):
        """Return (qs, collection) from qs (which may be a tuple)."""
        if hasattr(qs, 'model'):
            return (qs, self.get_collection(qs.model))
        elif isinstance(qs, (list, tuple)):
            return (qs[0], self.get_col_by_name(qs[1]))
        else:
            raise TypeError('Invalid query spec: %r' % qs)

    def sub_unique_objects(self, sub, params=None, pub=None, *args, **kwargs):
        """Return objects that are only visible through given subscription."""
        if params is None:
            params = sub.params

        params[0].running_from="do_sub"
        if pub is None:
            pub = self.get_pub_by_name(sub.publication)
        queries = collections.OrderedDict(
            (col, qs) for (qs, col) in (
                self.qs_and_collection(qs)
                for qs
                in pub.user_queries(sub.user, *params)
            )
        )
        # mergebox via MVCC!  For details on how this is possible, read this:
        # https://devcenter.heroku.com/articles/postgresql-concurrency
        to_send = collections.OrderedDict(
            (
                col,
                col.objects_for_user(
                    user=sub.user,
                    qs=qs,
                    *args, **kwargs
                ),
            )
            for col, qs
            in queries.items()
        )
        for other in Subscription.objects.filter(
                connection=sub.connection_id,
                collections__collection_name__in=[col.name for col in queries],
        ).exclude(
            pk=sub.pk,
        ).order_by('pk').distinct():
            other_pub = self.get_pub_by_name(other.publication)
            for qs in other_pub.user_queries(other.user, *other.params):
                qs, col = self.qs_and_collection(qs)
                if col not in to_send:
                    continue
                to_send[col] = to_send[col].exclude(
                    pk__in=col.objects_for_user(
                        user=other.user,
                        qs=qs,
                        *args, **kwargs
                    ).values('pk'),
                )
        for col, qs in to_send.items():
            yield col, qs.distinct()

    @api_endpoint
    def sub(self, id_, name, *params):
        """Create subscription, send matched objects that haven't been sent."""
        return self.do_sub(id_, name, False, *params)

    def do_sub(self, id_, name, silent, *params):
        """Subscribe the current thread to the specified publication."""
        try:
            pub = self.get_pub_by_name(name)
        except KeyError:
            if not silent:
                raise MeteorError(404, 'Subscription not found')
            return
        sub, created = Subscription.objects.get_or_create(
            connection_id=this.ws.connection.pk,
            sub_id=id_,
            user_id=getattr(this, 'user_id', None),
            defaults={
                'publication': pub.name,
                'params_ejson': params,
            },
        )
        this.subs.setdefault(sub.publication, set()).add(sub.pk)
        if not created:
            if not silent:
                this.send({'msg': 'ready', 'subs': [id_]})
            return
        # re-read from DB so we can get transaction ID (xmin)
        sub = Subscription.objects.extra(**XMIN).get(pk=sub.pk)
        for col, qs in self.sub_unique_objects(
                sub, sub.params, pub, xmin__lte=sub.xmin,
        ):
            sub.collections.create(
                model_name=model_name(qs.model),
                collection_name=col.name,
            )
            if isinstance(col.model._meta.pk, AleaIdField):
                meteor_ids = None
            elif len([
                field
                for field
                in col.model._meta.local_fields
                if (
                    isinstance(field, AleaIdField)
                ) and (
                    field.unique
                ) and (
                    not field.null
                )
            ]) == 1:
                meteor_ids = None
            else:
                meteor_ids = get_meteor_ids(
                    qs.model, qs.values_list('pk', flat=True),
                )
            for obj in qs.select_related():
                payload = col.obj_change_as_msg(obj, ADDED, meteor_ids)
                this.send(payload)
        if not silent:
            this.send({'msg': 'ready', 'subs': [id_]})

    @api_endpoint
    def unsub(self, id_):
        """Remove a subscription."""
        self.do_unsub(id_, False)

    def do_unsub(self, id_, silent):
        """Unsubscribe the current thread from the specified subscription id."""
        sub = Subscription.objects.get(
            connection=this.ws.connection, sub_id=id_,
        )
        # for col, qs in self.sub_unique_objects(sub):
        #     if isinstance(col.model._meta.pk, AleaIdField):
        #         meteor_ids = None
        #     else:
        #         meteor_ids = get_meteor_ids(
        #             qs.model, qs.values_list('pk', flat=True),
        #         )
        #     for obj in qs:
        #         payload = col.obj_change_as_msg(obj, REMOVED, meteor_ids)
        #         this.send(payload)
        this.subs[sub.publication].remove(sub.pk)
        sub.delete()
        if not silent:
            this.send({'msg': 'nosub', 'id': id_})

    @api_endpoint(decorate=False)
    def method(self, method, params, id_):
        """Invoke a method."""
        try:
            handler = self.api_path_map()[method]
        except KeyError:
            raise MeteorError(404, 'Method not found', method)
        if isinstance(params,dict):
            result = handler(**params)
        else:
            try:
                inspect.getcallargs(handler, *params)
            except TypeError as err:
                raise MeteorError(400, '%s' % err)
            result = handler(*params)

        msg = {'msg': 'result', 'id': id_}
        if result is not None:
            msg['result'] = result
        this.send(msg)

    def register(self, api_or_iterable):
        """Register an API endpoint."""
        if hasattr(api_or_iterable, 'api_path_prefix'):
            api_or_iterable = [api_or_iterable]
        for api in api_or_iterable:
            api = api()
            if api.api_path_prefix in self._registry:
                raise AlreadyRegistered(
                    'API with prefix %r is already registered to %r' % (
                        api.api_path_prefix,
                        self._registry[api.api_path_prefix],
                    ),
                )
            self._registry[api.api_path_prefix] = api
            self.clear_api_path_map_cache()
            if isinstance(api, Collection):
                self._model_cols[api.model].append(api)
                if api.aid_field:
                    self._model_aid[api.model]=api.aid_field

    @api_endpoint
    def schema(self):
        """Return schema for all registered collections."""
        res = {}
        for api_provider in self.api_providers:
            if isinstance(api_provider, Collection):
                collection = api_provider
                res[model_name(collection.model)] = collection.schema()
        return res

    def ready(self):
        """Initialisation for django-ddp (setup lookups and signal handlers)."""
        for model in self._model_cols:
            # set/unset self._in_migration
            signals.pre_migrate.connect(self.on_pre_migrate,sender=model)
            signals.post_migrate.connect(self.on_post_migrate,sender=model)
            # update self._ddp_subscribers before changes made
            signals.pre_delete.connect(self.on_pre_change,sender=model)
            signals.pre_save.connect(self.on_pre_change,sender=model)
            # emit change message after changes made
            signals.post_save.connect(self.on_post_save,sender=model)
            signals.post_delete.connect(self.on_post_delete,sender=model)
            signals.m2m_changed.connect(self.on_m2m_changed,sender=model)
            # call ready on each registered API endpoint
        for api_provider in self.api_providers:
            api_provider.ready()

    def on_pre_migrate(self, sender, **kwargs):
        """Pre-migrate signal handler."""
        self._in_migration = True

    def on_post_migrate(self, sender, **kwargs):
        """Post-migrate signal handler."""
        self._in_migration = False
        try:
            Connection.objects.all().delete()
        except DatabaseError:  # pylint: disable=E0712
            pass

    def on_pre_change(self, sender, **kwargs):
        """Pre change (save/delete) signal handler."""
        if self._in_migration:
            return
        if model_name(sender).split('.', 1)[0] in ('migrations', 'dddp'):
            return  # never send migration or DDP internal models
        obj = kwargs['instance']
        using = kwargs['using']
        if obj.pk:
            self._ddp_subscribers.setdefault(
                using, {},
            ).setdefault(
                sender, {},
            )[obj.pk] = self.valid_subscribers(
                model=sender, obj=obj, using=using,
            )

    def on_m2m_changed(self, sender, **kwargs):
        """M2M-changed signal handler."""
        if self._in_migration:
            return
        if kwargs['reverse'] is False:
            objs = [kwargs['instance']]
            model = objs[0].__class__
        else:
            model = kwargs['model']
            objs = model.objects.filter(pk__in=kwargs['pk_set'])
        mod_name = model_name(model)
        if mod_name.split('.', 1)[0] in ('migrations', 'dddp'):
            return  # never send migration or DDP internal models
        # See https://docs.djangoproject.com/en/1.7/ref/signals/#m2m-changed
        if kwargs['action'] in (
                'pre_add',
                'pre_remove',
                'pre_clear',
        ):
            for obj in objs:
                self.on_pre_change(
                    sender=model, instance=obj, using=kwargs['using'],
                )
        elif kwargs['action'] in (
                'post_add',
                'post_remove',
                'post_clear',
        ):

            for obj in objs:
                self.send_notify(
                    model=model,
                    obj=obj,
                    msg=CHANGED,
                    using=kwargs['using'],
                )

    def on_post_save(self, sender, **kwargs):
        """Post-save signal handler."""
        if self._in_migration:
            return
        self.send_notify(
            model=sender,
            obj=kwargs['instance'],
            msg=kwargs['created'] and ADDED or CHANGED,
            using=kwargs['using'],
        )

    def on_post_delete(self, sender, **kwargs):
        """Post-delete signal handler."""
        if self._in_migration:
            return
        self.send_notify(
            model=sender,
            obj=kwargs['instance'],
            msg=REMOVED,
            using=kwargs['using'],
        )

    def valid_subscribers(self,model,obj,using):
        col_connection_ids = collections.defaultdict(set)
        user_q=Q()
        cols=self._model_cols[model]
        for col in cols:
            col_q=Q()

            if not col.reversed_user_rel:
                continue
            for reversed_user_rel in col.reversed_user_rel:
                col_q |= Q(**{"__".join(["user"]+filter(None,[reversed_user_rel])):obj})
            col_q=Q(collections__collection_name=col.name) & col_q
            user_q |= col_q
        for sub in Subscription.objects.filter(
            collections__model_name=model_name(model),
        ).filter(user_q).distinct("id").select_related("user"):
            pub = self.get_pub_by_name(sub.publication)
            try:
                queries = list(pub.user_queries(sub.user, *sub.params))
            except Exception:
                queries = []
            for qs, col in (
                    self.qs_and_collection(qs)
                    for qs
                    in queries
            ):
                # check if obj is an instance of the model for the queryset
                if qs.model is not model:
                    continue  # wrong model on queryset
                # check if obj is included in this subscription
                if qs.query.where and not qs.filter(pk=obj.pk).exists():
                    continue  # subscription doesn't include this obj

                col_connection_ids[col].add(sub.connection_id)

        return col_connection_ids

    def send_notify(self, model, obj, msg, using):
        """Dispatch PostgreSQL async NOTIFY."""
        if model_name(model).split('.', 1)[0] in ('migrations', 'dddp'):
            return  # never send migration or DDP internal models

        new_col_connection_ids = self.valid_subscribers(model, obj, using)
        old_col_connection_ids = self._ddp_subscribers.get(
            using, {},
        ).get(
            model, {},
        ).pop(
            obj.pk, collections.defaultdict(set),
        )
        try:
            my_connection_id = this.ws.connection.pk
        except AttributeError:
            my_connection_id = None
        meteor_ids = {}
        all_connection_ids=set(itertools.chain(*old_col_connection_ids.values())) | set(itertools.chain(*new_col_connection_ids.values()))
        connection_ids_pids=dict(Connection.objects.filter(pk__in=all_connection_ids).values_list("pk","pid"))
        cursor = connections[using].cursor()
        for col in set(old_col_connection_ids).union(new_col_connection_ids):
            old_connection_ids = old_col_connection_ids[col]
            new_connection_ids = new_col_connection_ids[col]
            for (msg, connection_ids) in (
                    (REMOVED, old_connection_ids - new_connection_ids),
                    (CHANGED, old_connection_ids & new_connection_ids),
                    (ADDED, new_connection_ids - old_connection_ids),
            ):
                if not connection_ids:
                    continue  # nobody subscribed
                pids_connection_ids=collections.defaultdict(list)
                for connection_id in connection_ids:
                    pids_connection_ids[connection_ids_pids[connection_id]].append(connection_id)

                for pid,connection_ids in pids_connection_ids.iteritems():
                    payload = col.obj_change_as_msg(obj, msg, meteor_ids)
                    payload['_connection_ids'] = sorted(connection_ids)
                    if my_connection_id is not None:
                        payload['_sender'] = my_connection_id
                        if my_connection_id in connection_ids:
                            # msg must go to connection that initiated the change
                            payload['_tx_id'] = this.ws.get_tx_id()
                    # header is sent in every payload
                    header = {
                        'uuid': uuid.uuid1().int,  # UUID1 should be unique
                        'seq': 1,  # increments for each 8KB chunk
                        'fin': 0,  # zero if more chunks expected, 1 if last chunk.
                    }
                    data = ejson.dumps(payload)
                    while data:
                        hdr = ejson.dumps(header)
                        # use all available payload space for chunk
                        max_len = 8000 - len(hdr) - 100
                        # take a chunk from data
                        chunk, data = data[:max_len], data[max_len:]
                        if not data:
                            # last chunk, set fin=1.
                            header['fin'] = 1
                            hdr = ejson.dumps(header)
                        # print('NOTIFY: %s' % hdr)
                        cursor.execute(
                            'NOTIFY "ddp-%s", %%s' % pid,
                            [
                                '%s|%s' % (hdr, chunk),  # pipe separates hdr|chunk.
                            ],
                        )
                        header['seq'] += 1  # increment sequence.

    def login_required(self,func):
        @wraps(func)
        def wrap(*args,**kwargs):
            if not (getattr(this,"user",None) and this.user.pk):
                raise MeteorError(403,"Not login")
            return func(*args,**kwargs)
        return wrap

    def send_to_users(self,users,msgs,using="default"):
        cursor = connections[using].cursor()

        try:
            my_connection_id = this.ws.connection.pk
        except AttributeError:
            my_connection_id = None

        connection_ids_pids=collections.defaultdict(list)
        for con_id,pid in Subscription.objects.filter(user__in=users,publication="LoggedInUser").values_list("connection","connection__pid"):
            connection_ids_pids[pid].append(con_id)

        for msg in msgs:
            for pid,connection_ids in connection_ids_pids.items():
                header = {
                    'uuid': uuid.uuid1().int,  # UUID1 should be unique
                    'seq': 1,  # increments for each 8KB chunk
                    'fin': 0,  # zero if more chunks expected, 1 if last chunk.
                }
                payload=dict(msg,_connection_ids=sorted(connection_ids))
                if my_connection_id is not None:
                    payload['_sender'] = my_connection_id
                    if my_connection_id in connection_ids:
                        # msg must go to connection that initiated the change
                        payload['_tx_id'] = this.ws.get_tx_id()
                data = ejson.dumps(payload)
                while data:
                    hdr = ejson.dumps(header)
                    # use all available payload space for chunk
                    max_len = 8000 - len(hdr) - 100
                    # take a chunk from data
                    chunk, data = data[:max_len], data[max_len:]
                    if not data:
                        # last chunk, set fin=1.
                        header['fin'] = 1
                        hdr = ejson.dumps(header)
                    # print('NOTIFY: %s' % hdr)
                    cursor.execute(
                        'NOTIFY "ddp-%s", %%s' % pid,
                        [
                            '%s|%s' % (hdr, chunk),  # pipe separates hdr|chunk.
                        ],
                    )
                    header['seq'] += 1  # increment sequence.

API = DDP()
