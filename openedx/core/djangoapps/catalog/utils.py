"""Helper functions for working with the catalog service."""
import copy
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from edx_rest_api_client.client import EdxRestApiClient

from openedx.core.djangoapps.catalog.cache import PROGRAM_CACHE_KEY_TPL, PROGRAM_UUIDS_CACHE_KEY
from openedx.core.djangoapps.catalog.models import CatalogIntegration
from openedx.core.lib.edx_api_utils import get_edx_api_data
from openedx.core.lib.token_utils import JwtBuilder


logger = logging.getLogger(__name__)
User = get_user_model()  # pylint: disable=invalid-name


def create_catalog_api_client(user, catalog_integration):
    """Returns an API client which can be used to make catalog API requests."""
    scopes = ['email', 'profile']
    expires_in = settings.OAUTH_ID_TOKEN_EXPIRATION
    jwt = JwtBuilder(user).build_token(scopes, expires_in)

    return EdxRestApiClient(catalog_integration.internal_api_url, jwt=jwt)


def get_programs(uuid=None):
    """Read programs from the cache.

    The cache is populated by a management command, cache_programs.

    Keyword Arguments:
        uuid (string): UUID identifying a specific program to read from the cache.

    Returns:
        list of dict, representing programs.
        dict, if a specific program is requested.
    """
    missing_details_msg_tpl = 'Failed to get details for program {uuid} from the cache.'

    if uuid:
        program = cache.get(PROGRAM_CACHE_KEY_TPL.format(uuid=uuid))
        if not program:
            logger.warning(missing_details_msg_tpl.format(uuid=uuid))

        return program

    uuids = cache.get(PROGRAM_UUIDS_CACHE_KEY, [])
    if not uuids:
        logger.warning('Failed to get program UUIDs from the cache.')

    programs = cache.get_many([PROGRAM_CACHE_KEY_TPL.format(uuid=uuid) for uuid in uuids])
    programs = list(programs.values())

    # The get_many above sometimes fails to bring back details cached on one or
    # more Memcached nodes. It doesn't look like these keys are being evicted.
    # 99% of the time all keys come back, but 1% of the time all the keys stored
    # on one or more nodes are missing from the result of the get_many. One
    # get_many may fail to bring these keys back, but a get_many occurring
    # immediately afterwards will succeed in bringing back all the keys. This
    # behavior can be mitigated by trying again for the missing keys, which is
    # what we do here. Splitting the get_many into smaller chunks may also help.
    missing_uuids = set(uuids) - set(program['uuid'] for program in programs)
    if missing_uuids:
        logger.info(
            'Failed to get details for {count} programs. Retrying.'.format(count=len(missing_uuids))
        )

        retried_programs = cache.get_many([PROGRAM_CACHE_KEY_TPL.format(uuid=uuid) for uuid in missing_uuids])
        programs += list(retried_programs.values())

        still_missing_uuids = set(uuids) - set(program['uuid'] for program in programs)
        for uuid in still_missing_uuids:
            logger.warning(missing_details_msg_tpl.format(uuid=uuid))

    return programs


def get_program_types(name=None):
    """Retrieve program types from the catalog service.

    Keyword Arguments:
        name (string): Name identifying a specific program.

    Returns:
        list of dict, representing program types.
        dict, if a specific program type is requested.
    """
    catalog_integration = CatalogIntegration.current()
    if catalog_integration.enabled:
        try:
            user = User.objects.get(username=catalog_integration.service_username)
        except User.DoesNotExist:
            return []

        api = create_catalog_api_client(user, catalog_integration)
        cache_key = '{base}.program_types'.format(base=catalog_integration.CACHE_KEY)

        data = get_edx_api_data(catalog_integration, 'program_types', api=api,
                                cache_key=cache_key if catalog_integration.is_cache_enabled else None)

        # Filter by name if a name was provided
        if name:
            data = next(program_type for program_type in data if program_type['name'] == name)

        return data
    else:
        return []


def get_programs_with_type(types=None):
    """
    Return the list of programs. You can filter the types of programs returned using the optional
    types parameter. If no filter is provided, all programs of all types will be returned.

    The program dict is updated with the fully serialized program type.

    Keyword Arguments:
        types (list): List of program type slugs to filter by.

    Return:
        list of dict, representing the active programs.
    """
    programs_with_type = []
    programs = get_programs()

    if programs:
        program_types = {program_type['name']: program_type for program_type in get_program_types()}
        for program in programs:
            # This limited type filtering is sufficient for current needs and
            # helps us avoid additional complexity when caching program data.
            # However, if the need for additional filtering of programs should
            # arise, consider using the cache_programs management command to
            # cache the filtered lists of UUIDs.
            if types and program['type'] not in types:
                continue

            # deepcopy the program dict here so we are not adding
            # the type to the cached object
            program_with_type = copy.deepcopy(program)
            program_with_type['type'] = program_types[program['type']]
            programs_with_type.append(program_with_type)

    return programs_with_type


def get_course_runs():
    """
    Retrieve all the course runs from the catalog service.

    Returns:
        list of dict with each record representing a course run.
    """
    catalog_integration = CatalogIntegration.current()
    course_runs = []
    if catalog_integration.enabled:
        try:
            user = User.objects.get(username=catalog_integration.service_username)
        except User.DoesNotExist:
            logger.error(
                'Catalog service user with username [%s] does not exist. Course runs will not be retrieved.',
                catalog_integration.service_username,
            )
            return course_runs

        api = create_catalog_api_client(user, catalog_integration)

        querystring = {
            'page_size': catalog_integration.page_size,
            'exclude_utm': 1,
        }

        course_runs = get_edx_api_data(catalog_integration, 'course_runs', api=api, querystring=querystring)

    return course_runs
