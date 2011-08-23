# -*- coding: utf-8 -*-
# Copyright (c) 2010, 2011, Sebastian Wiesner <lunaryorn@googlemail.com>
# All rights reserved.

# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:

# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.

# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.


"""
    sphinxcontrib.issuetracker
    ==========================

    Integration with isse trackers.

    Replace textual issue ids (like #1) with a reference to the
    corresponding issue in an issue tracker.

    .. moduleauthor::  Sebastian Wiesner  <lunaryorn@googlemail.com>
"""

from __future__ import (print_function, division, unicode_literals,
                        absolute_import)


__version__ = '0.8'

import re
import urllib
from contextlib import closing
from collections import namedtuple
from os import path

from docutils import nodes
from docutils.transforms import Transform
from sphinx.addnodes import pending_xref
from sphinx.util.osutil import copyfile
from sphinx.util.console import bold


Issue = namedtuple('Issue', 'id title uri closed')

class TrackerConfig(namedtuple('_TrackerConfig', 'project')):

    @classmethod
    def from_sphinx_config(cls, config):
        project = config.issuetracker_project or config.project
        return cls(project)


def fetch_issue(app, url, output_format=None):
    """
    Fetch issue data from a web service or website.

    ``app`` is the sphinx application object.  ``url`` is the url of the issue.
    ``output_format`` is the format of the data retrieved from the given
    ``url``.  If set, it must be either ``'json'`` or ``'xml'``.

    Return the raw data retrieved from url, if ``output_format`` is unset.  If
    ``output_format`` is ``'xml'``, return a ElementTree document.  If
    ``output_format`` is ``'json'``, return the object parsed from JSON
    (typically a dictionary).  Return ``None``, if ``url`` returned a status
    code other than 200.
    """
    if output_format not in ('json', 'xml'):
        raise ValueError(output_format)

    with closing(urllib.urlopen(url)) as response:
        if response.getcode() == 404:
            return None
        elif response.getcode() != 200:
            # warn about unexpected response code
            app.warn('{0} unavailable with code {1}'.format(
                url, response.getcode()))
            return None
        if output_format == 'json':
            import json
            return json.load(response)
        elif output_format == 'xml':
            from xml.etree import cElementTree as etree
            return etree.parse(response)
        else:
            return response


def check_project_with_username(tracker_config):
    if '/' not in tracker_config.project:
        raise ValueError(
            'username missing in tracker_config {0.project!r}'.format(
                tracker_config))


GITHUB_API_URL = 'https://api.github.com/repos/{0.project}/issues/{1}'

def lookup_github_issue(app, tracker_config, issue_id):
    check_project_with_username(tracker_config)

    url = GITHUB_API_URL.format(tracker_config, issue_id)
    issue = fetch_issue(app, url, output_format='json')
    if issue:
        closed = issue['state'] == 'closed'
        return Issue(id=issue_id, title=issue['title'], closed=closed,
                     uri=issue['html_url'])


BITBUCKET_URL = 'https://bitbucket.org/{0.project}/issue/{1}/'
BITBUCKET_API_URL = ('https://api.bitbucket.org/1.0/repositories/'
                     '{0.project}/issues/{1}/')

def lookup_bitbucket_issue(app, tracker_config, issue_id):
    check_project_with_username(tracker_config)

    url = BITBUCKET_API_URL.format(tracker_config, issue_id)
    issue = fetch_issue(app, url, output_format='json')
    if issue:
        closed = issue['status'] not in ('new', 'open')
        uri=BITBUCKET_URL.format(tracker_config, issue_id)
        return Issue(id=issue_id, title=issue['title'], closed=closed, uri=uri)


DEBIAN_URL = 'http://bugs.debian.org/cgi-bin/bugreport.cgi?bug={0}'

def lookup_debian_issue(app, tracker_config, issue_id):
    import debianbts
    try:
        # get the bug
        bug = debianbts.get_status(issue_id)[0]
    except IndexError:
        return None

    # check if issue matches project
    if tracker_config.project not in (bug.package, bug.source):
        return None

    return Issue(id=issue_id, title=bug.subject, closed=bug.done,
                 uri=DEBIAN_URL.format(issue_id))


LAUNCHPAD_URL = 'https://bugs.launchpad.net/bugs/{0}'

def lookup_launchpad_issue(app, tracker_config, issue_id):
    launchpad = getattr(app.env, 'issuetracker_launchpad', None)
    if not launchpad:
        from launchpadlib.launchpad import Launchpad
        launchpad = Launchpad.login_anonymously(
            'sphinxcontrib.issuetracker', service_root='production')
        app.env.issuetracker_launchpad = launchpad
    try:
        # get the bug
        bug = launchpad.bugs[issue_id]
    except KeyError:
        return None

    for task in bug.bug_tasks:
        if task.bug_target_name == tracker_config.project:
            break
    else:
        # no matching task found
        return None

    return Issue(id=issue_id, title=task.title, closed=bool(task.date_closed),
                 uri=LAUNCHPAD_URL.format(issue_id))


GOOGLE_CODE_URL = 'http://code.google.com/p/{0.project}/issues/detail?id={1}'
GOOGLE_CODE_API_URL = ('http://code.google.com/feeds/issues/p/'
                       '{0.project}/issues/full/{1}')

def lookup_google_code_issue(app, tracker_config, issue_id):
    url = GOOGLE_CODE_API_URL.format(tracker_config, issue_id)
    issue = fetch_issue(app, url, output_format='xml')
    if issue:
        ISSUE_NS = '{http://schemas.google.com/projecthosting/issues/2009}'
        ATOM_NS = '{http://www.w3.org/2005/Atom}'
        state = issue.find('{0}state'.format(ISSUE_NS))
        title_node = issue.find('{0}title'.format(ATOM_NS))
        title = title_node.text if title_node is not None else None
        closed = state is not None and state.text == 'closed'
        return Issue(id=issue_id, title=title, closed=closed,
                     uri=GOOGLE_CODE_URL.format(tracker_config, issue_id))


JIRA_API_URL = 'http://{0.project}/si/jira.issueviews:issue-xml/{1}/{1}.xml'

def lookup_jira_issue(app, tracker_config, issue_id):
    issue = fetch_issue(app, JIRA_API_URL.format(tracker_config, issue_id),
                        output_format='xml')
    if issue:
        uri = issue.find('*/item/link').text
        state = issue.find('*/item/resolution').text
        title = issue.find('*/item/title').text
        closed = state.lower() != 'unresolved'
        return Issue(id=issue_id, title=title, closed=closed, uri=uri)


BUILTIN_ISSUE_TRACKERS = {
    'github': lookup_github_issue,
    'bitbucket': lookup_bitbucket_issue,
    'debian': lookup_debian_issue,
    'launchpad': lookup_launchpad_issue,
    'google code': lookup_google_code_issue,
    'jira': lookup_jira_issue,
    }


class IssuesReferences(Transform):
    """
    Parse and transform issue ids in a document.

    Issue ids are parsed in text nodes and transformed into
    :class:`~sphinx.addnodes.pending_xref` nodes for further processing in
    later stages of the build.
    """

    default_priority = 999

    def apply(self):
        config = self.document.settings.env.config
        tracker_config = TrackerConfig.from_sphinx_config(config)
        issue_pattern = config.issuetracker_issue_pattern
        if isinstance(issue_pattern, basestring):
            issue_pattern = re.compile(issue_pattern)
        for node in self.document.traverse(nodes.Text):
            parent = node.parent
            if isinstance(parent, (nodes.literal, nodes.FixedTextElement)):
                # ignore inline and block literal text
                continue
            text = unicode(node)
            new_nodes = []
            last_issue_ref_end = 0
            for match in issue_pattern.finditer(text):
                # catch invalid pattern with too many groups
                if len(match.groups()) != 1:
                    raise ValueError(
                        'issuetracker_issue_pattern must have '
                        'exactly one group: {0!r}'.format(match.groups()))
                # extract the text between the last issue reference and the
                # current issue reference and put it into a new text node
                head = text[last_issue_ref_end:match.start()]
                if head:
                    new_nodes.append(nodes.Text(head))
                # adjust the position of the last issue reference in the
                # text
                last_issue_ref_end = match.end()
                # extract the issue text (including the leading dash)
                issuetext = match.group(0)
                # extract the issue number (excluding the leading dash)
                issue_id = match.group(1)
                # turn the issue reference into a reference node
                refnode = pending_xref()
                refnode['reftarget'] = issue_id
                refnode['reftype'] = 'issue'
                refnode['trackerconfig'] = tracker_config
                refnode['expandtitle'] = config.issuetracker_expandtitle
                refnode.append(nodes.Text(issuetext))
                new_nodes.append(refnode)
            if not new_nodes:
                # no issue references were found, move on to the next node
                continue
            # extract the remaining text after the last issue reference, and
            # put it into a text node
            tail = text[last_issue_ref_end:]
            if tail:
                new_nodes.append(nodes.Text(tail))
            # find and remove the original node, and insert all new nodes
            # instead
            parent.replace(node, new_nodes)


def make_issue_reference(issue, content_node):
    """
    Create a reference node for the given issue.

    ``content_node`` is a docutils node which is supposed to be added as
    content of the created reference.  ``issue`` is the :class:`Issue` which
    the reference shall point to.

    Return a :class:`docutils.nodes.reference` for the issue.
    """
    reference = nodes.reference()
    reference['refuri'] = issue.uri
    if issue.closed:
        reference['classes'].append('issue-closed')
    reference['classes'].append('reference-issue')
    reference.append(content_node)
    return reference


def lookup_issue(app, tracker_config, issue_id):
    """
    Lookup the given issue.

    The issue is first looked up in an internal cache.  If it is not found, the
    event ``issuetracker-resolve-issue`` is emitted.  The result of this
    invocation is then cached and returned.

    ``app`` is the sphinx application object.  ``tracker_config`` is the
    :class:`TrackerConfig` object representing the issue tracker configuration.
    ``issue_id`` is a string containing the issue id.

    Return a :class:`Issue` object for the issue with the given ``issue_id``,
    or ``None`` if the issue wasn't found.
    """
    cache = app.env.issuetracker_cache
    issue = cache.get(issue_id)
    if not issue:
        issue = app.emit_firstresult('issuetracker-resolve-issue',
                                     tracker_config, issue_id)
        cache[issue_id] = issue
    return issue


def resolve_issue_references(app, doctree):
    """
    Resolve all pending issue references in the given ``doctree``.
    """
    for node in doctree.traverse(pending_xref):
        if node['reftype'] == 'issue':
            issue = lookup_issue(app, node['trackerconfig'], node['reftarget'])
            if not issue:
                new_node = node[0]
            else:
                if node['expandtitle'] and issue.title:
                    content_node = nodes.Text(issue.title)
                else:
                    content_node = node[0]
                new_node = make_issue_reference(issue, content_node)
            node.replace_self(new_node)


def connect_builtin_tracker(app):
    if app.config.issuetracker:
        app.connect(b'issuetracker-resolve-issue',
                    BUILTIN_ISSUE_TRACKERS[app.config.issuetracker.lower()])


def add_stylesheet(app):
    app.add_stylesheet('issuetracker.css')


def init_cache(app):
    if not hasattr(app.env, 'issuetracker_cache'):
        app.env.issuetracker_cache = {}


def copy_stylesheet(app, exception):
    if app.builder.name != 'html' or exception:
        return
    app.info(bold('Copying issuetracker stylesheet... '), nonl=True)
    dest = path.join(app.builder.outdir, '_static', 'issuetracker.css')
    source = path.join(path.abspath(path.dirname(__file__)),
                          'issuetracker.css')
    copyfile(source, dest)
    app.info('done')


def setup(app):
    app.require_sphinx('1.0')
    app.add_transform(IssuesReferences)
    app.add_event(b'issuetracker-resolve-issue')
    app.connect(b'builder-inited', connect_builtin_tracker)
    app.add_config_value('issuetracker_issue_pattern',
                         re.compile(r'#(\d+)'), 'env')
    app.add_config_value('issuetracker_project', None, 'env')
    app.add_config_value('issuetracker_expandtitle', False, 'env')
    app.add_config_value('issuetracker', None, 'env')
    app.connect(b'builder-inited', add_stylesheet)
    app.connect(b'builder-inited', init_cache)
    app.connect(b'doctree-read', resolve_issue_references)
    app.connect(b'build-finished', copy_stylesheet)
