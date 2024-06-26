from datetime import datetime
import dateutil.parser
from lxml import etree as ET
from osc import conf
from osc.core import show_project_meta
from osclib.core import devel_project_fallback
from osclib.core import request_age
from osclib.util import sha1_short
import re


class RequestSplitter(object):
    def __init__(self, api, requests, in_ring, stageable=True):
        self.api = api
        self.requests = requests
        self.in_ring = in_ring
        self.stageable = stageable
        self.config = conf.config[self.api.project]

        # 55 minutes to avoid two staging bot loops of 30 minutes
        self.request_age_threshold = int(self.config.get('splitter-request-age-threshold', 55 * 60))
        self.staging_age_max = int(self.config.get('splitter-staging-age-max', 8 * 60 * 60))
        special_packages = self.config.get('splitter-special-packages')
        if special_packages is not None:
            StrategySpecial.PACKAGES = special_packages.split(' ')

        self.requests_ignored = self.api.get_ignored_requests()

        self.reset()
        # after propose_assignment()
        self.proposal = {}

    def reset(self):
        self.strategy = None
        self.filters = []
        self.groups = []

        # after split()
        self.filtered = []
        self.other = []
        self.grouped = {}

        if self.stageable:
            # Require requests to be stageable (submit or delete package).
            self.filter_add('./action[@type="submit" or (@type="delete" and ./target[@package])]')

    def strategy_set(self, name, **kwargs):
        self.reset()

        class_name = f'Strategy{name.lower().title()}'
        cls = globals()[class_name]
        self.strategy = cls(**kwargs)
        self.strategy.apply(self)

    def strategy_from_splitter_info(self, splitter_info):
        strategy = splitter_info['strategy']
        if 'args' in strategy:
            self.strategy_set(strategy['name'], **strategy['args'])
        else:
            self.strategy_set(strategy['name'])

    def filter_add(self, xpath):
        self.filters.append(ET.XPath(xpath))

    def filter_add_requests(self, requests):
        requests = ' ' + ' '.join(requests) + ' '
        self.filter_add('contains("{requests}", concat(" ", @id, " ")) or '
                        'contains("{requests}", concat(" ", ./action/target/@package, " "))'
                        .format(requests=requests))

    def group_by(self, xpath, required=False):
        self.groups.append(ET.XPath(xpath))
        if required:
            self.filter_add(xpath)

    def is_staging_mergeable(self, staging):
        return self.stagings[staging]['status'].find('staged_requests/request') is not None

    def filter_only(self):
        ret = []
        for request in self.requests:
            self.supplement(request)
            if self.filter_check(request):
                ret.append(request)
        return ret

    def split(self):
        for request in self.requests:
            self.supplement(request)
            if not self.filter_check(request):
                continue

            ring = request.find('./action/target').get('ring')
            if self.in_ring != (not ring):
                # Request is of desired ring type.
                key = self.group_key_build(request)
                if key not in self.grouped:
                    self.grouped[key] = {
                        'bootstrap_required': False,
                        'requests': [],
                    }

                self.grouped[key]['requests'].append(request)

                if ring and ring.startswith('0'):
                    self.grouped[key]['bootstrap_required'] = True
            else:
                self.other.append(request)

    def supplement(self, request):
        """ Provide additional information for grouping """
        if request.get('ignored'):
            # Only supplement once.
            return

        history = request.find('history')
        if history is not None:
            age = request_age(request).total_seconds()
            request.set('aged', str(age >= self.request_age_threshold))

        request_type = request.find('./action').get('type')
        target = request.find('./action/target')
        target_project = target.get('project')
        target_package = target.get('package')
        devel, _ = devel_project_fallback(self.api.apiurl, target_project, target_package)
        if not devel and request_type == 'submit':
            devel = request.find('./action/source').get('project')
        if devel:
            target.set('devel_project', devel)
            StrategySuper.supplement(request)

        ring = self.ring_get(target_package)
        if ring:
            target.set('ring', ring)

        request_id = int(request.get('id'))
        if request_id in self.requests_ignored:
            request.set('ignored', str(self.requests_ignored[request_id]))
        else:
            request.set('ignored', 'False')

        request.set('postponed', 'False')

    def ring_get(self, target_package):
        if self.api.conlyadi:
            return None
        if self.api.crings:
            ring = self.api.ring_packages_for_links.get(target_package)
            if ring:
                # Cut off *:Rings: prefix.
                return ring[len(self.api.crings) + 1:]
        else:
            # Projects not using rings handle all requests as ring requests.
            return self.api.project
        return None

    def filter_check(self, request):
        for xpath in self.filters:
            if not xpath(request):
                return False
        return True

    def group_key_build(self, request):
        if len(self.groups) == 0:
            return 'all'

        key = []
        for xpath in self.groups:
            element = xpath(request)
            if element:
                key.append(element[0])
        if len(key) == 0:
            return '00'
        return '__'.join(key)

    def should_staging_merge(self, staging):
        staging = self.stagings[staging]
        if (not staging['bootstrapped'] and
            staging['splitter_info']['strategy']['name'] in ('devel', 'super') and
                staging['status'].get('state') not in ('acceptable', 'review')):
            # Simplistic attempt to allow for followup requests to be staged
            # after age max has been passed while still stopping when ready.
            return True

        if 'activated' not in staging['splitter_info']:
            # No information on the age of the staging.
            return False

        # Allows for immediate staging when possible while not blocking requests
        # created shortly after. This method removes the need to wait to create
        # a larger staging at once while not ending up with lots of tiny
        # stagings. As such this handles both high and low request backlogs.
        activated = dateutil.parser.parse(staging['splitter_info']['activated'])
        delta = datetime.utcnow() - activated
        return delta.total_seconds() <= self.staging_age_max

    def is_staging_considerable(self, staging):
        staging = self.stagings[staging]
        if staging['status'].find('staged_requests/request') is not None:
            return False
        return self.api.prj_frozen_enough(staging['project'])

    def stagings_load(self, stagings):
        self.stagings = {}
        self.stagings_considerable = []
        self.stagings_mergeable = []
        self.stagings_mergeable_none = []

        # Use specified list of stagings, otherwise only empty, letter stagings.
        if len(stagings) == 0:
            whitelist = self.config.get('splitter-whitelist')
            if whitelist:
                stagings = whitelist.split()
            else:
                stagings = self.api.get_staging_projects_short()
            should_always = False
        else:
            # If the an explicit list of stagings was included then always
            # attempt to use even if the normal conditions are not met.
            should_always = True

        for staging in stagings:
            project = self.api.prj_from_short(staging)
            status = self.api.project_status(project)
            bootstrapped = self.api.is_staging_bootstrapped(project)

            # Store information about staging.
            self.stagings[staging] = {
                'project': project,
                'bootstrapped': bootstrapped,
                # TODO: find better place for splitter info
                'splitter_info': {'strategy': {'name': 'none'}},
                'status': status
            }

            # Decide if staging of interested.
            if self.is_staging_mergeable(staging) and (should_always or self.should_staging_merge(staging)):
                if self.stagings[staging]['splitter_info']['strategy']['name'] == 'none':
                    self.stagings_mergeable_none.append(staging)
                else:
                    self.stagings_mergeable.append(staging)
            elif self.is_staging_considerable(staging):
                self.stagings_considerable.append(staging)

        # Allow both considered and remaining to be accessible after proposal.
        self.stagings_available = list(self.stagings_considerable)

        return (len(self.stagings_considerable) +
                len(self.stagings_mergeable) +
                len(self.stagings_mergeable_none))

    def propose_assignment(self):
        # Attempt to assign groups that have bootstrap_required first.
        for group in sorted(self.grouped.keys()):
            if self.grouped[group]['bootstrap_required']:
                staging = self.propose_staging(choose_bootstrapped=True)
                if staging:
                    self.requests_assign(group, staging)
                else:
                    self.requests_postpone(group)

        # Assign groups that do not have bootstrap_required and fallback to a
        # bootstrapped staging if no non-bootstrapped stagings available.
        for group in sorted(self.grouped.keys()):
            if not self.grouped[group]['bootstrap_required']:
                staging = self.propose_staging(choose_bootstrapped=False)
                if staging:
                    self.requests_assign(group, staging)
                    continue

                staging = self.propose_staging(choose_bootstrapped=True)
                if staging:
                    self.requests_assign(group, staging)
                else:
                    self.requests_postpone(group)

    def requests_assign(self, group, staging, merge=False):
        # Arbitrary, but descriptive group key for proposal.
        key = f'{len(self.proposal)}#{self.strategy.key}@{group}'
        self.proposal[key] = {
            'bootstrap_required': self.grouped[group]['bootstrap_required'],
            'group': group,
            'requests': {},
            'staging': staging,
            'strategy': self.strategy.info(),
        }
        if merge:
            self.proposal[key]['merge'] = True

        # Covert request nodes to simple proposal form.
        for request in self.grouped[group]['requests']:
            self.proposal[key]['requests'][int(request.get('id'))] = request.find('action/target').get('package')
            self.requests.remove(request)

        return key

    def requests_postpone(self, group):
        if self.strategy.name == 'none':
            return

        for request in self.grouped[group]['requests']:
            request.set('postponed', 'True')

    def propose_staging(self, choose_bootstrapped):
        found = False
        for staging in sorted(self.stagings_available):
            if choose_bootstrapped == self.stagings[staging]['bootstrapped']:
                found = True
                break

        if found:
            self.stagings_available.remove(staging)
            return staging

        return None

    def strategies_try(self):
        strategies = (
            'special',
            'quick',
            'super',
            'devel',
        )

        for strategy in strategies:
            self.strategy_try(strategy)

    def strategy_try(self, name):
        self.strategy_set(name)
        self.split()

        groups = self.strategy.desirable(self)
        if len(groups) == 0:
            return
        self.filter_grouped(groups)

        self.propose_assignment()

    def strategy_do(self, name, **kwargs):
        self.strategy_set(name, **kwargs)
        self.split()
        self.propose_assignment()

    def strategy_do_non_bootstrapped(self, name, **kwargs):
        self.strategy_set(name, **kwargs)
        self.filter_add('./action/target[not(starts-with(@ring, "0"))]')
        self.split()
        self.propose_assignment()

    def filter_grouped(self, groups):
        for group in sorted(self.grouped.keys()):
            if group not in groups:
                del self.grouped[group]

    def merge_staging(self, staging):
        staging = self.stagings[staging]
        splitter_info = staging['splitter_info']
        self.strategy_from_splitter_info(splitter_info)

        if not self.stagings[staging]['bootstrapped']:
            # If when the strategy was first run the resulting staging was not
            # bootstrapped then ensure no bootstrapped packages are included.
            self.filter_add('./action/target[not(starts-with(@ring, "0"))]')

        self.split()

        group = splitter_info['group']
        if group in self.grouped:
            self.requests_assign(group, staging, merge=True)

    def merge(self, strategy_none=False):
        stagings = self.stagings_mergeable_none if strategy_none else self.stagings_mergeable
        for staging in sorted(stagings):
            self.merge_staging(staging)


class Strategy(object):
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.name = self.__class__.__name__[8:].lower()
        self.key = self.name
        if kwargs:
            self.key += '_' + sha1_short(str(kwargs))

    def info(self):
        info = {'name': self.name}
        if self.kwargs:
            info['args'] = self.kwargs
        return info

    def desirable(self, splitter):
        return splitter.grouped.keys()


class StrategyNone(Strategy):
    def apply(self, splitter):
        # All other strategies that inherit this are not restricted by age as
        # the age restriction is used to allow other strategies to be observed.
        if type(self) is StrategyNone:
            splitter.filter_add('@aged="True"')
        splitter.filter_add('@ignored="False"')
        splitter.filter_add('@postponed="False"')


class StrategyRequests(Strategy):
    def apply(self, splitter):
        splitter.filter_add_requests(self.kwargs['requests'])


class StrategyCustom(StrategyNone):
    def apply(self, splitter):
        if 'filters' not in self.kwargs:
            super(StrategyCustom, self).apply(splitter)
        else:
            for xpath in self.kwargs['filters']:
                splitter.filter_add(xpath)

        if 'groups' in self.kwargs:
            for group in self.kwargs['groups']:
                splitter.group_by(group)


class StrategyDevel(StrategyNone):
    GROUP_MIN = 7
    GROUP_MIN_MAP = {
        'YaST:Head': 2,
    }

    def apply(self, splitter):
        super(StrategyDevel, self).apply(splitter)
        splitter.group_by('./action/target/@devel_project', True)

    def desirable(self, splitter):
        groups = []
        for group, info in sorted(splitter.grouped.items()):
            if len(info['requests']) >= self.GROUP_MIN_MAP.get(group, self.GROUP_MIN):
                groups.append(group)
        return groups


class StrategySuper(StrategyDevel):
    # Regex pattern prefix representing super devel projects that should be
    # grouped together. The whole pattern will be used and the last colon
    # stripped, otherwise the first match group.
    PATTERNS = [
        'KDE:',
        'GNOME:',
        '(multimedia):(?:libs|apps)',
        'zypp:'
    ]

    @classmethod
    def init(cls):
        cls.patterns = []
        for pattern in cls.PATTERNS:
            cls.patterns.append(re.compile(pattern))

    @classmethod
    def supplement(cls, request):
        if not hasattr(cls, 'patterns'):
            cls.init()

        target = request.find('./action/target')
        devel_project = target.get('devel_project')
        for pattern in cls.patterns:
            match = pattern.match(devel_project)
            if match:
                prefix = match.group(0 if len(match.groups()) == 0 else 1).rstrip(':')
                target.set('devel_project_super', prefix)
                break

    def apply(self, splitter):
        super(StrategySuper, self).apply(splitter)
        splitter.groups = []
        splitter.group_by('./action/target/@devel_project_super', True)


class StrategyQuick(StrategyNone):
    def apply(self, splitter):
        super(StrategyQuick, self).apply(splitter)

        # Origin manager accepted which means any extra reviews have been added.
        splitter.filter_add('./review[@by_user="origin-manager" and @state="accepted"]')

        # No @by_project reviews that are not accepted. If not first round stage
        # this should also ignore previous staging project reviews or already
        # accepted human reviews.
        splitter.filter_add('not(./review[@by_project and @state!="accepted"])')

        # Only allow reviews by whitelisted groups and users as all others will
        # be considered non-quick (like @by_group="legal-auto"). The allowed
        # groups are only those configured as reviewers on the target project.
        meta = ET.fromstringlist(show_project_meta(splitter.api.apiurl, splitter.api.project))
        allowed_groups = meta.xpath('group[@role="reviewer"]/@groupid')
        self.filter_review_whitelist(splitter, 'by_group', allowed_groups)

    def filter_review_whitelist(self, splitter, attribute, allowed):
        # Rather than generate a bunch of @attribute="allowed[0]" pairs
        # contains is used, but the attribute must be asserted first since
        # concat() loses that requirement.
        allowed = ' ' + ' '.join(allowed) + ' '
        splitter.filter_add(
            # Assert that no(non-whitelisted and not accepted) review is found.
            'not(./review[@{attribute} and '
            'not(contains("{allowed}", concat(" ", @{attribute}, " "))) and '
            '@state!="accepted"])'.format(attribute=attribute, allowed=allowed))


class StrategySpecial(StrategyNone):
    # Configurable via splitter-special-packages.
    PACKAGES = [
        'gcc',
        'gcc8',
        'glibc',
        'kernel-source',
    ]

    def apply(self, splitter):
        super(StrategySpecial, self).apply(splitter)
        splitter.filter_add_requests(self.PACKAGES)
        splitter.group_by('./action/target/@package')
