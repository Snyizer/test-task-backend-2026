import inspect
import time
from typing import Any, Iterator, Type

import requests

TRANSACTIONS = "transactions"
TASKS = "tasks"
CUSTOMERS = "customers"
COMPANIES = "companies"
CONTACTS = "contacts"
CUSTOM_FIELDS = "custom_fields"
LEADS = "leads"
TAGS = "tags"
PIPELINES = "pipelines"
STATUSES = "statuses"
NOTES = "notes"
EVENTS = "events"


class AmoCrmRepeatError(Exception):
    """Надо ещё раз запрос кинуть"""


class AmoCrmNoConten(Exception):
    """
    amocrm error not found
    """


class AmoCrmSearchError(Exception):
    """
    amocrm error when search
    """


class AmoCrmError(Exception):
    errors = {
        "301": 'Moved permanently',
        "400": 'Bad request',
        "401": 'Unauthorized',
        "403": 'Forbidden',
        "404": 'Not found',
        "500": 'Internal server error',
        "502": 'Bad gateway',
        "503": 'Service unavailable',
        "429": 'Requests Per Second Exceeded'
    }

    def __init__(self, msg=None, error_code=None):
        self.error_code = error_code
        self.error_description = self.errors.get(str(error_code), f'Undescribed error: {error_code}')
        self.error_description = self.errors.get(str(error_code), f"Undescribed error: {error_code}")
        self.msg = f"{msg or 'AmoCRM error'}, {self.error_description}"
        super().__init__(self.msg)


class AmoCrmCompaniesMoreThanOne(Exception):
    """
    amocrm error when found companies > 1
    """


def normalize_custom_fields(raw: Any) -> dict:
    """Приводит custom_fields_values API к словарю {имя: {id, value}}."""
    if not raw:
        return {}
    if isinstance(raw, dict):
        normalized = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                normalized[key] = value
            else:
                normalized[key] = {"value": value}
        return normalized
    if not isinstance(raw, list):
        return {}

    normalized = {}
    for field in raw:
        if not isinstance(field, dict):
            continue
        name = field.get("field_name") or field.get("field_code") or field.get("name")
        if not name:
            continue
        values = field.get("values") or []
        value = None
        if isinstance(values, list) and values and isinstance(values[0], dict):
            value = values[0].get("value")
        elif "value" in field:
            value = field.get("value")
        normalized[name] = {"id": field.get("field_id", field.get("id")), "value": value}
    return normalized


def should_fetch_next_page(counter: int, limit: int, page, next_href) -> bool:
    """Полная страница без ссылки next — конец выборки, а не ошибка."""
    if page or counter < limit or not next_href:
        return False
    return True


def next_page_href(json_data: dict):
    links = json_data.get("_links") or {}
    nxt = links.get("next")
    if not isinstance(nxt, dict):
        return None
    return nxt.get("href") or None


def _assign_embedded(entity, embedded, with_tags=False):
    entity.companies = []
    entity.contacts = []
    if with_tags:
        entity.tags = []
    if not embedded:
        return
    if embedded.get(COMPANIES):
        entity.companies = embedded.get(COMPANIES)
    if embedded.get(CONTACTS):
        entity.contacts = embedded.get(CONTACTS)
    if with_tags and embedded.get(TAGS):
        entity.tags = embedded.get(TAGS)


class _EntitySchema:
    def __init__(self, obj_cls: Type["BaseEntity"], many: bool = False):
        self.obj_cls = obj_cls
        self.many = many

    def load(self, data):
        if self.many:
            return [self._one(item) for item in data]
        return self._one(data)

    def _one(self, item):
        payload = dict(item or {})
        payload["custom_fields_values"] = normalize_custom_fields(payload.get("custom_fields_values"))
        signature = inspect.signature(self.obj_cls.__init__)
        kwargs = {}
        for name, param in signature.parameters.items():
            if name == "self":
                continue
            if name in payload:
                kwargs[name] = payload[name]
            elif param.default is inspect.Parameter.empty:
                kwargs[name] = None
        return self.obj_cls(**kwargs)


class BaseEntity:
    """
    base class for entity
    """
    schema = None
    entity_name = None
    url = None
    custom_fields_values = None
    PATCH_FIELDS = None

    def __str__(self):
        return f"<{type(self).__name__}: {self.name}, {self.id}>"

    def __repr__(self):
        return str(self)

    @classmethod
    def get_schema(cls, many: bool = False) -> _EntitySchema:
        return _EntitySchema(cls, many=many)

    def as_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if not key.startswith("_")}

    def get_field(self, field_name):
        fields = self.custom_fields_values
        if not isinstance(fields, dict):
            fields = normalize_custom_fields(fields)
            self.custom_fields_values = fields
        field = fields.get(field_name) or {}
        if isinstance(field, dict):
            return field.get('value')
        return None

    def set_field(self, field_name, field_value, field_id=None):
        if not isinstance(self.custom_fields_values, dict):
            self.custom_fields_values = normalize_custom_fields(self.custom_fields_values)
        custom_field = self.custom_fields_values.get(field_name)
        if custom_field is None:
            if field_id is None:
                raise KeyError(f"Not found custom field with key: {field_name}")
            self.custom_fields_values[field_name] = dict(id=field_id, value=field_value)
            return
        if custom_field.get('id'):
            custom_field['value'] = field_value
        elif field_id:
            custom_field['value'] = field_value
            custom_field['id'] = field_id
        else:
            raise ValueError(f"Custom field {field_name} without id")

    def get_url(self):
        return self.url


class Customer(BaseEntity):
    """
    class for amocrm customer
    """
    url = "api/v4/customers"
    entity_name = CUSTOMERS

    def __init__(self,
                 id,
                 name,
                 next_price,
                 next_date,
                 responsible_user_id,
                 status_id,
                 periodicity,
                 created_by,
                 updated_by,
                 created_at,
                 updated_at,
                 closest_task_at,
                 is_deleted,
                 ltv,
                 purchases_count,
                 average_check,
                 account_id,
                 custom_fields_values,
                 _embedded=None
                 ):
        self.id = id
        self.name = name
        self.next_price = next_price
        self.next_date = next_date
        self.responsible_user_id = responsible_user_id
        self.status_id = status_id
        self.periodicity = periodicity
        self.created_by = created_by
        self.updated_by = updated_by
        self.created_at = created_at
        self.updated_at = updated_at
        self.closest_task_at = closest_task_at
        self.is_deleted = is_deleted
        self.ltv = ltv
        self.purchases_count = purchases_count
        self.average_check = average_check
        self.account_id = account_id
        self.custom_fields_values = custom_fields_values if isinstance(custom_fields_values, dict) else normalize_custom_fields(custom_fields_values)
        self._embedded = _embedded
        self._new_next_date = None
        _assign_embedded(self, _embedded)


class Lead(BaseEntity):
    """
    class for amocrm lead
    """
    url = "api/v4/leads"
    entity_name = LEADS
    PATCH_FIELDS = ("custom_fields_values", "id")

    def __init__(self,
                 name=None,
                 id=None,
                 price=None,
                 responsible_user_id=None,
                 group_id=None,
                 status_id=None,
                 pipeline_id=None,
                 source_id=None,
                 created_by=None,
                 updated_by=None,
                 created_at=None,
                 updated_at=None,
                 closest_task_at=None,
                 is_deleted=None,
                 score=None,
                 account_id=None,
                 custom_fields_values=None,
                 _embedded=None
                 ):
        self.id = id
        self.name = name
        self.price = price
        self.responsible_user_id = responsible_user_id
        self.group_id = group_id
        self.status_id = status_id
        self.pipeline_id = pipeline_id
        self.source_id = source_id
        self.created_by = created_by
        self.updated_by = updated_by
        self.created_at = created_at
        self.updated_at = updated_at
        self.closest_task_at = closest_task_at
        self.is_deleted = is_deleted
        self.score = score
        self.account_id = account_id
        self.custom_fields_values = custom_fields_values if isinstance(custom_fields_values, dict) else normalize_custom_fields(custom_fields_values)
        self._embedded = _embedded
        _assign_embedded(self, _embedded, with_tags=True)


def amocrm_get(obj_cls: Type[BaseEntity],
               page: int = None,
               limit: int = 250,
               _filter: dict = None,
               _with: str = None,
               query: Any = None,
               custom_filter: dict = None) -> Iterator[BaseEntity]:
    params = {}
    if page:
        params['page'] = page
    if limit:
        params['limit'] = limit
    if _filter:
        for key, value in _filter.items():
            filter_key = f"filter[{key}]"
            if isinstance(value, list):
                filter_key = f"{filter_key}[]"
            params[filter_key] = value
    if _with:
        if isinstance(_with, list):
            params['with'] = ",".join(_with)
        else:
            params['with'] = _with
    if query:
        params['query'] = query

    headers = {}
    base_url = ""
    url = f"{base_url}{obj_cls.url}"
    entity_name = obj_cls.entity_name
    schema = obj_cls.get_schema(many=True)

    while True:
        response = None
        json_data = None
        try:
            for try_attempt in range(4):  # 0..3 — до 4 попыток на один запрос
                try:
                    response = requests.get(url, params=params, headers=headers, timeout=120)
                    json_data = response_handler(response)
                    break
                except AmoCrmRepeatError as e:
                    if try_attempt >= 3:
                        raise e
                    # повторить тот же запрос (тот же url, params), без нового get() с начала
                except AmoCrmNoConten:
                    raise
                except Exception as e:
                    raise AmoCrmError(f"[!] {response.status_code if response else '<No code!>'} |"
                                      f" Неожиданная AMOCRM ошибка {e} ") from e
        except AmoCrmNoConten:
            break
        if json_data is not None:
            counter = 0
            for entity in schema.load(json_data['_embedded'][entity_name]):
                counter += 1
                skip_entity = False
                if custom_filter:
                    # if custom filter is not None then check custom field,
                    # if at least one not equal filter, set skip_entity value True and break checker
                    #
                    for key, value in custom_filter.items():
                        if entity.get_field(key) != value:
                            skip_entity = True
                            break
                # check if should pass entity
                if skip_entity:
                    pass
                else:
                    yield entity

            # condition to stop while loop
            href = next_page_href(json_data)
            if not should_fetch_next_page(counter, limit, page, href):
                break
            time.sleep(1)
            # reset url and params for page iteration
            url = href
            params = {}


def response_handler(response):
    if response.status_code == 204:
        raise AmoCrmNoConten(f"no content for {response.url}, method: {response.request.method}")
    elif response.status_code == 429:
        time.sleep(2)
        raise AmoCrmRepeatError()
    elif response.status_code != 200:
        json_data = {}
        try:
            json_data = response.json()
        except Exception:
            pass
        raise AmoCrmError(error_code=response.status_code, msg=json_data)
    return response.json()
