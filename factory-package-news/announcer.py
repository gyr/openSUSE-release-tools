#!/usr/bin/python
# Copyright (c) 2014 SUSE Linux Products GmbH
# Copyright (c) 2015, 2016, 2017 SUSE Linux GmbH
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import httplib
import re
from urlparse import urlparse, urljoin
import smtplib
from email.mime.text import MIMEText
import os
import sys
import email.utils
import argparse
import logging
import yaml
from xdg.BaseDirectory import save_data_path
from collections import namedtuple

logger = logging.getLogger()

# map of default config entries
config_defaults = {
    'sender': None,
    'to': None,
    'relay': None,
    'url' : "http://download.opensuse.org/tumbleweed/iso/",
    'iso' : "openSUSE-Tumbleweed-DVD-x86_64-Current.iso",
    'name' : 'factory-announcer',
    'subject' : 'New Tumbleweed snapshot {version} released!',
    'changesfile' : "Changes.{version}.txt",
    'bodytemplate' : """
Please note that this mail was generated by a script.
The described changes are computed based on the x86_64 DVD.
The full online repo contains too many changes to be listed here.

Please check the known defects of this snapshot before upgrading:
https://openqa.opensuse.org/tests/overview?distri=opensuse&groupid=1&version=Tumbleweed&build={version}

When you reply to report some issues, make sure to change the subject.
It is not helpful to keep the release announcement subject in a thread
while discussing a specific problem.

{text}
""",

}

def _load_config(handle = None):
    d = config_defaults
    y = yaml.safe_load(handle) if handle is not None else {}
    return namedtuple('Config', sorted(d.keys()))(*[ y.get(p, d[p]) for p in sorted(d.keys()) ])

parser = argparse.ArgumentParser(description="Announce new snapshots")
parser.add_argument("--dry", action="store_true", help="dry run")
parser.add_argument("--debug", action="store_true", help="debug output")
parser.add_argument("--verbose", action="store_true", help="verbose")
parser.add_argument("--from", dest='sender', metavar="EMAIL", help="sender email address")
parser.add_argument("--to", metavar="EMAIL", help="recepient email address")
parser.add_argument("--relay", metavar="RELAY", help="SMTP relay server address")
parser.add_argument("--version", metavar="VERSION", help="announce specific version")
parser.add_argument("--config", metavar="FILE", type=argparse.FileType(), help="YAML config file to override defaults")
parser.add_argument("--dump-config", action="store_true", help="dump built in YAML config")

options = parser.parse_args()

# Set logging configuration
logging.basicConfig(level=logging.DEBUG if options.debug
                    else logging.INFO,
                    format='%(asctime)s - %(module)s:%(lineno)d - %(levelname)s - %(message)s')

if options.dump_config:
    print yaml.dump(config_defaults, default_flow_style=False)
    sys.exit(0)

config = _load_config(options.config)

if not options.sender:
    options.sender = config.sender
if not options.to:
    options.to = config.to
if not options.relay:
    options.relay = config.relay

if not options.sender or not options.to or not options.relay:
    logger.error("need to specify --from and --to and --relay")
    sys.exit(1)

datadir = save_data_path('opensuse.org', config.name)

current_fn = os.path.join(datadir, "announcer-current-version")

if not options.version:
    u = urlparse(urljoin(config.url, config.iso))
    conn = httplib.HTTPConnection(u.hostname, 80)
    conn.request('HEAD', u.path)
    res = conn.getresponse()
    if res.status != 302:
        raise Exception("http fail: %s %s" % (res.status, res.reason))

    loc = res.getheader('location')
    if loc is None:
        raise Exception("empty location!")

    m = re.search('(?:Snapshot|Build)(\d+)-Media', loc)
    if m is None:
        raise Exception("failed to parse %s"%loc)

    version = m.group(1)
    logger.debug("found version %s", version)
else:
    version = options.version

if os.path.lexists(current_fn):
    prev = os.readlink(current_fn)
    if prev == version:
        logger.debug("version unchanged, exit")
        sys.exit(0)

u = urlparse(urljoin(config.url, config.changesfile.format(version=version)))
conn = httplib.HTTPConnection(u.hostname, 80)
conn.request('HEAD', u.path)
res = conn.getresponse()
if res.status == 302:

    loc = res.getheader('location')
    if loc is None:
        raise Exception("empty location!")
    u = urlparse(loc)

conn = httplib.HTTPConnection(u.hostname, 80)
conn.request('GET', u.path)
res = conn.getresponse()
if res.status != 200:
    raise Exception("http fail: %s %s" % (res.status, res.reason))

txt = res.read()
if "====" not in txt:
    logger.error("no changes or file corrupt? not sending anything")
    sys.exit(1)

msg = MIMEText(config.bodytemplate.format(version=version, text=txt))
msg['Subject'] = config.subject.format(version=version)
msg['From'] = options.sender
msg['To'] = options.to
msg['Mail-Followup-To'] = options.to
msg['Date'] = email.utils.formatdate(localtime=1)
msg['Message-ID'] = email.utils.make_msgid()

if options.dry:
    print "sending ..."
    print msg.as_string()
else:
    logger.info("announcing version {}".format(version))
    s = smtplib.SMTP(options.relay)
    s.sendmail(options.sender, [msg['To']], msg.as_string())
    s.quit()

    tmpfn = os.path.join(datadir, ".announcer-current-version")
    os.symlink(version, tmpfn)
    os.rename(tmpfn, current_fn)

# vim: sw=4 et
