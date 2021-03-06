# unable to import webapp2
# pylint: disable=F0401

# don't worry about docstrings because of all the webapp functions
# pylint: disable=C0111

# likewise, __init__ are optional
# pylint: disable=W0232

# self.data doesn't exist (yes it does)
# pylint: disable=E1101

# I don't care how few methods my class has
# pylint: disable=R0903

# don't worry about the variables 'app' or 'base_app' name
# pylint: disable=C0103

# don't worry about 2 spaces indention
# pylint: disable=W0311

import os,re

from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.ext.db import stats
from google.appengine.api.labs import taskqueue
from google.appengine.api import images

from urlparse import urlparse
from urlparse import urlunparse
from urlparse import urljoin
from datetime import *

from libs.counter import counter
from libs.beautifulsoup import BeautifulSoup

from PIL import Image
from libs import Win32IconImagePlugin
from StringIO import StringIO

from globals import *
from models import *

import jinja2
import webapp2
import urllib2

JINJA_ENVIRONMENT = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.join(os.path.dirname(__file__), "../templates")),
    autoescape=True
)

class BaseHandler(webapp2.RequestHandler):

  def htc(self,m):
    return chr(int(m.group(1),16))

  def urldecode(self,url):
    rex=re.compile('%([0-9a-hA-H][0-9a-hA-H])',re.M)
    return rex.sub(self.htc,url)

  def printTemplate(self,templateFile,templateVars):

    # Find the full system path
    template = JINJA_ENVIRONMENT.get_template("%s.html" % (templateFile))

    # Write it out
    self.response.out.write(template.render(templateVars))

  def isDev(self):
    return os.environ.get("SERVER_SOFTWARE","").startswith("Development")

  def headlessDenial(self):
    self.error(404)


class deleteAll(BaseHandler):

  def get(self):

    if self.isDev():

      memcache.flush_all()

      allfavIconQuery = favIcon.all()
      favIcons = allfavIconQuery.fetch(250)
      db.delete(favIcons)


class cleanup(BaseHandler):

  def get(self):

    for i in range(5):
      taskqueue.add(
        queue_name='doCleanup',
        url='/_doCleanup',
        method='GET'
      )

    # Update Counts
    counter.UpdateDSCounters()


class doCleanup(BaseHandler):

  def get(self):

    # Cleanup DS cache
    iconCacheCleanQuery = favIcon.gql("where dateCreated < :1",datetime.now()-timedelta(days=DS_CACHE_TIME))
    iconCacheCleanResults = iconCacheCleanQuery.fetch(100)
    db.delete(iconCacheCleanResults)
    inf("Deleted %d old icon caches in DS" % len(iconCacheCleanResults))


class IndexPage(BaseHandler):

  def get(self):

    if HEADLESS:

      self.headlessDenial()

    else:

      # Last served icons query
      lastServedIconsQuery = favIcon.gql("where useDefault = False order by dateCreated desc")
      lastServedIcons = lastServedIconsQuery.fetch(22)

      # Retrieve counters
      favIconsServed = counter.GetCount("favIconsServed")
      favIconsServedDefault = counter.GetCount("favIconsServedDefault")
      iconFromCache = counter.GetCount("cacheMC") + counter.GetCount("cacheDS")
      iconNotFromCache = counter.GetCount("cacheNone")
      error = counter.GetCount("error")
      errorCached = counter.GetCount("errorMC")

      # Datastore stats
      if stats.GlobalStat.all().get():
        iconsCached = stats.GlobalStat.all().get().count
      else:
        iconsCached = 0

      # Icon calculations
      favIconsServedM = round(float(favIconsServed) / 1000000,2)
      iconsCachedM = round(float(iconsCached) / 1000000,2)
      try:
          percentReal = round(float(favIconsServedDefault) / float(favIconsServed) * 100,2)
          percentCache = round(float(iconFromCache) / float(iconFromCache + iconNotFromCache) * 100,2)
          percentErrorCache = round(float(errorCached) / float(error) * 100,2)
      except ZeroDivisionError:
          percentReal = 0
          percentCache = 0
          percentErrorCache = 0

      self.printTemplate("index",{
        "isHomepage":True,
        "favIconsServed":favIconsServedM,
        "percentReal":percentReal,
        "percentCache":percentCache,
        "error":error,
        "percentErrorCache":percentErrorCache,
        "iconsCached":iconsCachedM,
        "lastServedIcons":lastServedIcons
      })


class Decache(BaseHandler):

  def get(self):

    domain = self.request.get("domain")
    memcache.delete("icon-" + domain)

    deleteQuery = db.GqlQuery("SELECT __key__ FROM favIcon WHERE domain = :1", domain)
    db.delete(deleteQuery.fetch(100))


class TestPage(BaseHandler):

  def get(self):

    if HEADLESS:

      self.headlessDenial()

    else:

      topSites = []
      topSitesFile = open("topsites.txt")

      for line in topSitesFile:
        topSites.append(line.replace("\n",""))

      self.printTemplate("test",{
        "isHomepage":False,
        "topSites":topSites,
        "isDev":self.isDev()
      })


class PrintFavicon(BaseHandler):

  def isValidIconResponse(self,iconResponse):

    iconLength = len(iconResponse.content)

    iconContentType = iconResponse.headers.get("Content-Type")
    if iconContentType:
      iconContentType = iconContentType.split(";")[0]

    invalidIconReason = []

    inf("Icon: {}, {}, {}".format(iconContentType, iconLength, iconResponse.status_code))
    if not iconResponse.status_code == 200:
      invalidIconReason.append("Status code isn't 200")

    if iconContentType in ICON_MIMETYPE_BLACKLIST:
      invalidIconReason.append("Content-Type in ICON_MIMETYPE_BLACKLIST")

    if iconLength < MIN_ICON_LENGTH:
      invalidIconReason.append("Length below MIN_ICON_LENGTH")

    if iconLength > MAX_ICON_LENGTH:
      invalidIconReason.append("Length greater than MAX_ICON_LENGTH")

    if len(invalidIconReason) > 0:
      inf("Invalid icon because: %s" % invalidIconReason)
      return False
    else:
      return True


  def iconInMC(self):

    mcIcon = memcache.get("icon-" + self.targetDomain)

    if mcIcon:

      inf("Found icon MC cache")

      counter.ChangeCount("cacheMC",1)
      self.response.headers['X-Cache'] = "Hit from MC"

      if mcIcon == "ERROR":
        self.error(True)

        return True

      elif mcIcon == "DEFAULT":

        self.writeDefault(True)

        return True

      else:

        self.icon = mcIcon
        self.writeIcon()

        return True

    return False


  def iconInDS(self):

    iconCacheQuery = favIcon.gql("where domain = :1",self.targetDomain)
    iconCache = iconCacheQuery.fetch(1)

    if len(iconCache) > 0:

      inf("Found icon DS cache")

      counter.ChangeCount("cacheDS",1)
      self.response.headers['X-Cache'] = "Hit from DS"

      if iconCache[0].useDefault:

        self.writeDefault(True)
        return True

      else:

        self.icon = iconCache[0].icon

        self.cacheIcon(["MC"])
        self.writeIcon()

        return True

    return False

  def usingIcoPlugin(self):
    imageTypeId = Win32IconImagePlugin.Win32IconImageFile.format.upper()
    Image.OPEN[imageTypeId] = Win32IconImagePlugin.Win32IconImageFile, Win32IconImagePlugin._accept

  def processIcon(self):
    self.usingIcoPlugin()
    ico = Image.open(StringIO(self.icon))
    if 'sizes' in ico.info:
      sizes = ico.info['sizes']
      size = max(sizes)
      ico.size = size
    output = StringIO()
    ico.save(output, "PNG")
    self.icon = output.getvalue()
    output.close()

  def fallback(self):
    rootDomain = self.targetURL[1].split('.')
    rootDomain = '.'.join(rootDomain[-2:])
    overridePath = os.path.join(os.path.dirname(__file__), "../overrides/%s.png" % rootDomain)

    if os.path.exists(overridePath):
      inf("Found override")
      self.icon = open(overridePath,'r').read()
      self.writeIcon()

      return True

    fallbackCacheQuery = fallbackIcon.gql("where domain = :1",self.targetURL[1])
    fallbackIcons = fallbackCacheQuery.fetch(1)
    if len(fallbackIcons) > 0:
      self.icon = fallbackIcons[0].icon
      self.writeIcon()

      return True

    fallbackCacheQuery = fallbackIcon.gql("where domain = :1",rootDomain)
    fallbackIcons = fallbackCacheQuery.fetch(1)
    if len(fallbackIcons) > 0:
      self.icon = fallbackIcons[0].icon
      self.writeIcon()

      return True


    return False


  def iconAtRoot(self):

    rootIconPath = self.targetDomain + "/favicon.ico"

    inf("iconAtRoot, trying %s" % rootIconPath)

    try:

      rootDomainFaviconResult = urlfetch.fetch(
        url = rootIconPath,
        follow_redirects = True,
      )

    except:

      inf("Failed to retrieve iconAtRoot")

      return self.fallback()

    if self.isValidIconResponse(rootDomainFaviconResult):

      self.icon = rootDomainFaviconResult.content
      self.processIcon()
      self.cacheIcon()
      self.writeIcon()

      return True

    else:

      return self.fallback()

  def iconInDesktopPage(self):
    return self.iconInPage(False)

  def iconInPage(self, mobile=True, url=None):

    try:
      opener = urllib2.build_opener(urllib2.HTTPCookieProcessor())
      if mobile:
        opener.addheaders = [('User-agent', IPHONE_IOS7)]
      else:
        opener.addheaders = [('User-agent', CHROME_MAC)]
      if url:
        inf("iconInMobilePage, trying %s" % url)
        response = opener.open(url)
      else:
        inf("iconInPage, trying %s" % self.targetPath)
        response = opener.open(self.targetPath)
    except:

      inf("Failed to retrieve page to find icon")

      return False

    if response.getcode() == 200:

      try:

        pageSoup = BeautifulSoup.BeautifulSoup(response.read())
        pageSoupIcon = pageSoup.find("link",rel=re.compile(".*(apple-touch-icon).*",re.IGNORECASE))
        if pageSoupIcon is None:
          pageSoupIcon = pageSoup.find("link",rel=re.compile("^(shortcut|icon|shortcut icon)$",re.IGNORECASE),type=re.compile("^(image/png|image/gif)$",re.IGNORECASE))
        if pageSoupIcon is None:
          pageSoupIcon = pageSoup.find("link",rel=re.compile("^(shortcut|icon|shortcut icon)$",re.IGNORECASE))

      except:
        self.error()
        return False

      if pageSoupIcon:

        pageIconHref = pageSoupIcon.get("href")

        if pageIconHref:
          pageIconPath = urljoin(response.geturl(),pageIconHref)

          # stupid python urljoin implementation
          parts = urlparse(pageIconPath)
          partlist = list(parts)
          if partlist[2].startswith('/../'):
            while partlist[2].startswith('/../'):
              partlist[2] = partlist[2][3:]
          pageIconPath = urlunparse(tuple(partlist))

        else:

          inf("No icon found in page")
          return False

        inf("Found unconfirmed iconInPage at %s" % pageIconPath)

        try:

          pagePathFaviconResult = urlfetch.fetch(pageIconPath)

        except:

          inf("Failed to retrieve icon found in page")

          return False

        if self.isValidIconResponse(pagePathFaviconResult):

          self.icon = pagePathFaviconResult.content
          self.processIcon()
          self.cacheIcon()
          self.writeIcon()

          return True

    return False

  def iconOverridden(self):

    overridePath = os.path.join(os.path.dirname(__file__), "../overrides/%s.png" % self.targetURL[1])

    if self.targetURL[1].startswith('10') or self.targetURL[1].startswith('192'):
      if len(self.targetURL[1].split('.')) == 4:
        overridePath = os.path.join(os.path.dirname(__file__), "../overrides/router.png")


    if os.path.exists(overridePath):
      inf("Found override")
      self.icon = open(overridePath,'r').read()
      self.writeIcon()

      return True

    if self.targetURL[1].startswith('www') and len(self.targetURL[1].split('.')) == 3:
      overridePath = os.path.join(os.path.dirname(__file__), "../overrides/%s.png" % self.targetURL[1][4:])
      inf(overridePath)
      if os.path.exists(overridePath):
        inf("Found override")
        self.icon = open(overridePath,'r').read()
        self.writeIcon()

        return True

    return False


  def cacheIcon(self,cacheTo = ["DS","MC"]):

    inf("Caching to %s" % (cacheTo))

    # don't cache for dev server
    if self.isDev():
      inf("Don't cache for dev server")
      return

    # DS
    if "DS" in cacheTo:
      newFavicon = favIcon(
        domain = self.targetDomain,
        icon = self.icon,
        useDefault = False,
        referrer = self.request.headers.get("Referer")
      )
      newFavicon.put()

    # MC
    if "MC" in cacheTo:
      memcache.add("icon-" + self.targetDomain, self.icon, MC_CACHE_TIME)


  def writeHeaders(self):

    # MIME Type
    self.response.headers['Content-Type'] = "image/png"

    # CORS
    self.response.headers['Access-Control-Allow-Origin'] = "*"

    # Set caching headers
    self.response.headers['Cache-Control'] = "public, max-age=2592000"
    self.response.headers['Expires'] = (datetime.now()+timedelta(days=30)).strftime("%a, %d %b %Y %H:%M:%S %z")


  def writeIcon(self):

    inf("Writing icon length %d bytes" % (len(self.icon)))

    self.writeHeaders()

    # Write out icon
    self.response.out.write(self.icon)

  def error(self, fromCache=False):
    inf("Cache error in memcache")

    if not fromCache:
      memcache.add("icon-" + self.targetDomain, "ERROR", MC_ERROR_CACHE_TIME)
    else:
      counter.ChangeCount("errorMC",1)

    counter.ChangeCount("error",1)

    self.abort(404)


  def writeDefault(self, fromCache = False):

    inf("Writing default")

    self.abort(404)

    self.writeHeaders()

    if not fromCache:

      newFavicon = favIcon(
        domain = self.targetDomain,
        icon = None,
        useDefault = True,
        referrer = self.request.headers.get("Referer")
      )
      newFavicon.put()

      memcache.add("icon-" + self.targetDomain, "DEFAULT", MC_CACHE_TIME)

    counter.ChangeCount("favIconsServedDefault",1)

    if self.request.get("defaulticon"):

      if self.request.get("defaulticon") == "none":

        self.response.set_status(204)

      elif self.request.get("defaulticon") == "1pxgif":

        self.response.out.write(open("1px.gif").read())

      elif self.request.get("defaulticon") == "lightpng":

        self.response.out.write(open("default2.png").read())

      elif self.request.get("defaulticon") == "bluepng":

        self.response.out.write(open("default3.png").read())

      else:

        self.redirect(self.request.get("defaulticon"))

    else:

      self.response.out.write(open("default.gif").read())


  def get(self):

    counter.ChangeCount("favIconsServed",1)

    # Get page path
    self.targetPath = self.urldecode(self.request.path.lstrip("/"))

    inf("getFavicon for %s" % (self.targetPath))

    # Split path to get domain
    self.targetURL = urlparse(self.targetPath)
    if len(self.targetURL[0]) == 0:
      self.targetPath = "http://" + self.targetPath
      self.targetURL = urlparse(self.targetPath)
    self.targetDomain = "http://" + self.targetURL[1]

    inf("URL is %s" % (self.targetDomain))

    # Do we have an override?
    if not self.iconOverridden():

      # In MC?
      if not self.iconInMC():

        # In DS?
        if not self.iconInDS():

            counter.ChangeCount("cacheNone",1)

            # icon in m. page
            m_url = self.targetURL[1]
            if m_url.startswith("www") or len(m_url.split('.')) == 2:
              if m_url.startswith("www"):
                m_url = m_url[4:]
              m_url = "http://m." + m_url
              if self.iconInPage(True, m_url):
                return

            # Icon specified in page?
            if not self.iconInPage():

              # Icon specified in desktop page?
              if not self.iconInDesktopPage():

                # Icon at [domain]/favicon.ico?
                if not self.iconAtRoot():

                  self.error()

class customize(BaseHandler):
  def get(self):
    try:
      domainURL = str(self.request.get('url'))
      if (not domainURL.startswith('http://')) and (not domainURL.startswith('https://')):
        domainURL = "http://" + domainURL
      iconURL = str(self.request.get('icon'))
      domain = urlparse(domainURL)[1]
      response = urlfetch.fetch(iconURL)
      if self.isValidIconResponse(response):
          icon = response.content
          icon = self.processIcon(icon)
          newIcon = fallbackIcon(
            domain = domain,
            icon = icon,
          )
          newIcon.put()
          return self.response.write("<html><body><p>Done</p></body></html>")
    except:
      import sys
      print "Unexpected error:", sys.exc_info()[1]
      return self.response.write("<html><body><p>Error: " + str(sys.exc_info()[1]) + "</p></body></html>")
    self.response.write("<html><body><p>Invalid inputs</p></body></html>")

  def usingIcoPlugin(self):
    imageTypeId = Win32IconImagePlugin.Win32IconImageFile.format.upper()
    Image.OPEN[imageTypeId] = Win32IconImagePlugin.Win32IconImageFile, Win32IconImagePlugin._accept

  def processIcon(self, icon):
    self.usingIcoPlugin()
    ico = Image.open(StringIO(icon))
    if 'sizes' in ico.info:
      sizes = ico.info['sizes']
      size = max(sizes)
      ico.size = size
    output = StringIO()
    ico.save(output, "PNG")
    ico = output.getvalue()
    output.close()
    return ico

  def isValidIconResponse(self,iconResponse):

    iconLength = len(iconResponse.content)

    iconContentType = iconResponse.headers.get("Content-Type")
    if iconContentType:
      iconContentType = iconContentType.split(";")[0]

    invalidIconReason = []

    inf("Icon: {}, {}, {}".format(iconContentType, iconLength, iconResponse.status_code))
    if not iconResponse.status_code == 200:
      invalidIconReason.append("Status code isn't 200")

    if iconContentType in ICON_MIMETYPE_BLACKLIST:
      invalidIconReason.append("Content-Type in ICON_MIMETYPE_BLACKLIST")

    if iconLength < MIN_ICON_LENGTH:
      invalidIconReason.append("Length below MIN_ICON_LENGTH")

    if iconLength > MAX_ICON_LENGTH:
      invalidIconReason.append("Length greater than MAX_ICON_LENGTH")

    if len(invalidIconReason) > 0:
      inf("Invalid icon because: %s" % invalidIconReason)
      return False
    else:
      return True


application = webapp2.WSGIApplication(
  [
    ('/', IndexPage),
    ('/decache/', Decache),
    ('/test/', TestPage),
    ('/_cleanup', cleanup),
    ('/_doCleanup', doCleanup),
    ('/_deleteall', deleteAll),
    ('/_fallback', customize),
    ('/.*', PrintFavicon),
  ],
  debug=True
)
