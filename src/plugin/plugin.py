from json import load, dumps
from os import chmod, remove, system
from os.path import basename, exists
import threading
from queue import Queue

from enigma import eTimer, fbClass, eRCInput, getDesktop, eDVBVolumecontrol

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.AVSwitch import avSwitch
from Components.config import config, ConfigSubsection, ConfigYesNo
from Components.ActionMap import HelpableActionMap
from Components.Console import Console
from Components.PluginComponent import PluginDescriptor

from Components.ServiceEventTracker import InfoBarBase
from Components.ServiceEventTracker import ServiceEventTracker
from Components.Sources.StaticText import StaticText
from Components.SystemInfo import BoxInfo

from Screens.HelpMenu import HelpableScreen
from Screens.MessageBox import MessageBox
from Screens.Screen import Screen
from Screens.Setup import Setup
from Screens.Standby import QUIT_KODI, TryQuitMainloop

from Screens.InfoBarGenerics import InfoBarNotifications, InfoBarSeek, InfoBarAudioSelection, InfoBarShowHide, InfoBarSubtitleSupport
from Tools.BoundFunction import boundFunction
from Tools.Directories import fileWriteLine, fileReadLine
from Tools import Notifications

from .e2utils import InfoBarAspectChange, WebPixmap, MyAudioSelection, \
	StatusScreen, getPlayPositionInSeconds, getDurationInSeconds, \
	InfoBarSubservicesSupport
from enigma import eServiceReference, eTimer, ePythonMessagePump, \
	iPlayableService, fbClass, eRCInput, getDesktop, eDVBVolumecontrol
from .server import KodiExtRequestHandler, UDSServer

config.kodi = ConfigSubsection()
config.kodi.addToMainMenu = ConfigYesNo(False)
config.kodi.addToExtensionMenu = ConfigYesNo(True)
config.kodi.standalone = ConfigYesNo(False)

MACHINEBRAND = BoxInfo.getItem("displaybrand")

try:
	from Plugins.Extensions.SubsSupport import SubsSupport, SubsSupportStatus
except ImportError:
	class SubsSupport(object):
		def __init__(self, *args, **kwargs):
			pass

	class SubsSupportStatus(object):
		def __init__(self, *args, **kwargs):
			pass

(OP_CODE_EXIT,
OP_CODE_PLAY,
OP_CODE_PLAY_STATUS,
OP_CODE_PLAY_STOP,
OP_CODE_SWITCH_TO_ENIGMA2,
OP_CODE_SWITCH_TO_KODI) = range(6)
KODIRUN_SCRIPT = "unset PYTHONPATH;kodi;kodiext -T"
KODIRESUME_SCRIPT = "kodiext -P %s -K"
KODIEXT_SOCKET = "/tmp/kodiext.socket"
KODIEXTIN = "/tmp/kodiextin.json"
KODI_LAUNCHER = None

SESSION = None
SERVER = None
SERVER_THREAD = None

_g_dw, _g_dh = 1280, 720


class SetAudio:
	def __init__(self):
		self.VolPrev = 0
		self.VolPlayer = 0
		self.volctrl = eDVBVolumecontrol.getInstance()
		self.ac3 = "downmix"
		self.dts = "downmix"
		self.aac = "passthrough"
		self.aacplus = "passthrough"

	def read_audio_option(self, path, system_key, default):
		"""Read system audio configuration, if suported."""
		if BoxInfo.getItem(system_key):
			return fileReadLine(path, default)
		return default

	def write_audio_option(self, path, value, system_key):
		"""Write in the system, the audio configuration, if suported."""
		if BoxInfo.getItem(system_key):
			fileWriteLine(path, value)

	def switch(self, Tokodi=False, Player=False):
		"""Switch beetween audio profiles, assuring the volume is passed correctly."""
		if Tokodi:
			if Player:
				self.VolPlayer = self.volctrl.getVolume()
			vol = 100
			ac3, dts, aac, aacplus = "downmix", "downmix", "passthrough", "passthrough"
		else:
			if Player:
				vol = self.VolPlayer
			else:
				vol = self.VolPrev
			ac3, dts, aac, aacplus = self.ac3, self.dts, self.aac, self.aacplus

		self.volctrl.setVolume(vol, vol)
		self.write_audio_option("/proc/stb/audio/ac3", ac3, "CanDownmixAC3")
		self.write_audio_option("/proc/stb/audio/dts", dts, "CanDownmixDTS")
		self.write_audio_option("/proc/stb/audio/aac", aac, "CanDownmixAAC")
		self.write_audio_option("/proc/stb/audio/aacplus", aacplus, "CanDownmixAACPlus")

	def ReadData(self):
		self.VolPrev = self.volctrl.getVolume()
		self.VolPlayer = self.VolPrev
		self.ac3 = self.read_audio_option("/proc/stb/audio/ac3", "CanDownmixAC3", "downmix")
		self.dts = self.read_audio_option("/proc/stb/audio/dts", "CanDownmixDTS", "downmix")
		self.aac = self.read_audio_option("/proc/stb/audio/aac", "CanDownmixAAC", "passthrough")
		self.aacplus = self.read_audio_option("/proc/stb/audio/aacplus", "CanDownmixAACPlus", "passthrough")


class SetResolution:
	def __init__(self):
		self.E2res = None
		self.kodires = "720p"
		self.kodirate = "50Hz"
		self.port = config.av.videoport.value
		self.rate = None
		if MACHINEBRAND in ("Vu+", "Formuler"):
			resolutions = ("720i", "720p", "1080i", "1080p", "2160p", "2160p30")
		else:
			resolutions = ("720i", "720p", "1080i", "1080p", "2160p", "2160p30")
			rates = ("24Hz", "30Hz", "50Hz", "60Hz")
			for res in resolutions:
				for rate in rates:
					try:
						if avSwitch.isModeAvailable(self.port, res, rate):
							self.kodires = res
							self.kodirate = rate
					except Exception:
						pass

	def switch(self, Tokodi=False, Player=False):
		if Tokodi:
			if self.kodires and self.kodirate and self.port:
				avSwitch.setMode(self.port, self.kodires, self.kodirate)
				fileWriteLine("/proc/stb/video/videomode", self.kodires + self.kodirate.replace("Hz", ""))
		else:
			if self.E2res and self.rate and self.port:
				avSwitch.setMode(self.port, self.E2res, self.rate)

	def ReadData(self):
		self.E2res = config.av.videomode[self.port].value
		self.rate = config.av.videorate[self.E2res].value
		self.switch(True)


setaudio = SetAudio()
setresolution = SetResolution()


def SaveDesktopInfo():
	global _g_dw, _g_dh
	try:
		_g_dw = getDesktop(0).size().width()
		_g_dh = getDesktop(0).size().height()
	except Exception:
		_g_dw, _g_dh = 1280, 720
	print(f"[XBMC] Desktop size [{_g_dw}x{_g_dh}]")
	fileWriteLine("/tmp/dw.info", f"{_g_dw}x{_g_dh}")
	chmod("/tmp/dw.info", 0o755)


SaveDesktopInfo()


def esHD():
	if getDesktop(0).size().width() > 1400:
		return True
	else:
		return False


def fhd(num, factor=1.5):
	if esHD():
		prod = num * factor
	else:
		prod = num
	return int(round(prod))


def FBLock():
	print("[KodiLauncher] FBLock")
	fbClass.getInstance().lock()


def FBUnlock():
	print("[KodiLauncher] FBUnlock")
	fbClass.getInstance().unlock()


def RCLock():
	print("[KodiLauncher] RCLock")
	eRCInput.getInstance().lock()


def RCUnlock():
	print("[KodiLauncher] RCUnlock")
	eRCInput.getInstance().unlock()


def kodiStopped(data, retval, extraArgs):
	print(f"[KodiLauncher] kodi stopped: retval = {retval}")
	#KODI_LAUNCHER.stop()


def kodiResumeStopped(data, retval, extraArgs):
	print('[KodiLauncher] kodi resume script stopped: retval = %d' % retval)
	if retval > 0:
		KODI_LAUNCHER.stop()


class KodiVideoPlayer(InfoBarBase, InfoBarShowHide, SubsSupportStatus, SubsSupport, InfoBarSeek, InfoBarSubservicesSupport, InfoBarAspectChange, InfoBarAudioSelection, InfoBarNotifications, HelpableScreen, Screen):
	if esHD():
		skin = """
		<screen title="custom service source" position="0, 0" size="1921,1081" zPosition="1" flags="wfNoBorder" backgroundColor="transparent">
			<widget source="global.CurrentTime" render="Label" position="1700,34" size="150,67" font="RegularHD; 32" backgroundColor="#10000000" transparent="1" zPosition="3" halign="center">
			  <convert type="ClockToText">Default</convert>
			</widget>
			<eLabel name="" position="0,15" size="1924,125" zPosition="-10"/>
			<eLabel position="0,856" zPosition="-11" size="1921,224" />
			<widget name="image" position="30,780" size="300,300" alphatest="on" transparent="1"/>
			<widget source="session.CurrentService" render="Label" position="65,44" size="1845,38" zPosition="1"  font="RegularHD;24" valign="center" halign="left" foregroundColor="#00ffa533" transparent="1">
			  <convert type="ServiceName">Name</convert>
			</widget>
			<widget name="genre" position="65,86" size="1845,35" zPosition="2" font="RegularHD;19" valign="center" halign="left"/>
			<eLabel name="progressbar-back" position="343,900" size="1500,4" backgroundColor="#00cccccc" />
			<widget source="session.CurrentService" render="Progress" foregroundColor="#00007eff" backgroundColor="#00ffffff" position="343,897" size="1500,10" zPosition="7" transparent="0">
				<convert type="ServicePosition">Position</convert>
			</widget>
			<widget source="session.CurrentService" render="Label" position="750,935" size="180,67" zPosition="6" font="RegularHD;32" halign="left"   transparent="1">
				<convert type="ServicePosition">Position,ShowHours</convert>
			</widget>
			<eLabel name="" text="/" position="927,935" size="20,67" zPosition="6" font="RegularHD;32"/>
			<widget source="session.CurrentService" render="Label" position="952,935" size="180,67" zPosition="6" font="RegularHD;32" halign="left"   transparent="1">
				<convert type="ServicePosition">Length,ShowHours</convert>
			</widget>
			<ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/audio.png" position="1343,942" size="40,40" scale="1" alphatest="blend" />
				<ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/subtitle.png" position="1343,1007" size="40,40" scale="1" alphatest="blend" />
				<ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/info.png" position="740,1020" size="40,40" scale="1" alphatest="blend" />
				<ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/timeslip.png" position="925,1020" size="40,40" scale="1" alphatest="blend" />
		<eLabel name="" position="1130,940" size="200,45" transparent="1" text="Audio" halign="right" font="RegularHD; 20" />
		<eLabel name="" position="1130,1005" size="200,45" transparent="1" text="Subtitle" halign="right" font="RegularHD; 20" />
		<eLabel name="" position="790,1022" size="270,45" transparent="1" text="Info" font="RegularHD; 20" />
		<eLabel name="" position="975,1022" size="233,45" transparent="1" text="TimeSleep" font="RegularHD; 20" />
		<widget source="session.CurrentService" render="Label" position="1400,940" size="445,45" font="RegularHD; 20" backgroundColor="#001A1A1A">
				<convert type="TrackInfo">Audio</convert>
		</widget>
		<widget source="session.CurrentService" render="Label" position="1400,1005" size="445,45" font="RegularHD; 20" backgroundColor="#001E1C1C">
				<convert type="TrackInfo">Subtitle</convert>
		</widget>
		<widget source="session.CurrentService" render="Label" position="345,1013" size="90,30" font="RegularHD; 16" halign="right" valign="center" transparent="1">
				<convert type="ServiceInfo">VideoWidth</convert>
		</widget>
		<eLabel text="x" position="435,1013" size="24,30" font="RegularHD; 16" halign="center" valign="center" transparent="1" />
		<widget source="session.CurrentService" render="Label" position="462,1013" size="90,30" font="RegularHD; 16" halign="left" valign="center" transparent="1">
				<convert type="ServiceInfo">VideoHeight</convert>
		</widget>
		<widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/sd-ico.png" position="400,952" render="Pixmap" size="80,60" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
				<convert type="ServiceInfo">VideoWidth</convert>
				<convert type="ValueRange">0,720</convert>
				<convert type="ConditionalShowHide" />
		</widget>
		<widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/hd-ico.png" position="400,952" render="Pixmap" size="80,60" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
				<convert type="ServiceInfo">VideoWidth</convert>
				<convert type="ValueRange">721,1980</convert>
				<convert type="ConditionalShowHide" />
		</widget>
		<widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/4k-uhd-ico.png" position="400,952" render="Pixmap" size="80,60" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
				<convert type="ServiceInfo">VideoWidth</convert>
				<convert type="ValueRange">1921,4096</convert>
				<convert type="ConditionalShowHide" />
		</widget>
		</screen>"""
	else:
		skin = """
		<screen title="custom service source" position="0, 0" size="1280,720" zPosition="1" flags="wfNoBorder" backgroundColor="transparent">
			<widget source="global.CurrentTime" render="Label" position="1133,22" size="100,44" font="Regular; 32" backgroundColor="#10000000" transparent="1" zPosition="3" halign="center">
			  <convert type="ClockToText">Default</convert>
			</widget>
			<eLabel name="" position="0,10" size="1282,83" zPosition="-10"/>
			<eLabel position="0,570" zPosition="-11" size="1280,149" />
			<widget name="image" position="20,520" size="200,200" alphatest="on" transparent="1"/>
			<widget source="session.CurrentService" render="Label" position="43,29" size="1230,25" zPosition="1"  font="Regular;24" valign="center" halign="left" foregroundColor="#00ffa533" transparent="1">
			  <convert type="ServiceName">Name</convert>
			</widget>
			<widget name="genre" position="43,57" size="1230,23" zPosition="2" font="Regular;19" valign="center" halign="left"/>
			<eLabel name="progressbar-back" position="228,600" size="1000,2" backgroundColor="#00cccccc" />
			<widget source="session.CurrentService" render="Progress" foregroundColor="#00007eff" backgroundColor="#00ffffff" position="228,598" size="1000,6" zPosition="7" transparent="0">
				<convert type="ServicePosition">Position</convert>
			</widget>
			<widget source="session.CurrentService" render="Label" position="500,623" size="120,44" zPosition="6" font="Regular;32" halign="left"   transparent="1">
				<convert type="ServicePosition">Position,ShowHours</convert>
			</widget>
			<eLabel name="" text="/" position="618,623" size="13,44" zPosition="6" font="Regular;32"/>
			<widget source="session.CurrentService" render="Label" position="634,623" size="120,44" zPosition="6" font="Regular;32" halign="left"   transparent="1">
				<convert type="ServicePosition">Length,ShowHours</convert>
			</widget>
			<ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/audio.png" position="895,628" size="27,27" scale="1" alphatest="blend" />
				<ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/subtitle.png" position="895,671" size="27,27" scale="1" alphatest="blend" />
				<ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/info.png" position="493,680" size="27,27" scale="1" alphatest="blend" />
				<ePixmap pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/timeslip.png" position="617,680" size="27,27" scale="1" alphatest="blend" />
		<eLabel name="" position="753,627" size="133,30" transparent="1" text="Audio" halign="right" font="Regular; 20" />
		<eLabel name="" position="753,670" size="133,30" transparent="1" text="Subtitle" halign="right" font="Regular; 20" />
		<eLabel name="" position="527,681" size="180,30" transparent="1" text="Info" font="Regular; 20" />
		<eLabel name="" position="650,681" size="155,30" transparent="1" text="TimeSleep" font="Regular; 20" />
		<widget source="session.CurrentService" render="Label" position="933,627" size="297,30" font="Regular; 20" backgroundColor="#001A1A1A">
				<convert type="TrackInfo">Audio</convert>
		</widget>
		<widget source="session.CurrentService" render="Label" position="933,670" size="297,30" font="Regular; 20" backgroundColor="#001E1C1C">
				<convert type="TrackInfo">Subtitle</convert>
		</widget>
		<widget source="session.CurrentService" render="Label" position="230,675" size="60,20" font="Regular; 16" halign="right" valign="center" transparent="1">
				<convert type="ServiceInfo">VideoWidth</convert>
		</widget>
		<eLabel text="x" position="290,675" size="16,20" font="Regular; 16" halign="center" valign="center" transparent="1" />
		<widget source="session.CurrentService" render="Label" position="310,675" size="60,20" font="Regular; 16" halign="left" valign="center" transparent="1">
				<convert type="ServiceInfo">VideoHeight</convert>
		</widget>
		<widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/sd-ico.png" position="267,635" render="Pixmap" size="53,40" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
				<convert type="ServiceInfo">VideoWidth</convert>
				<convert type="ValueRange">0,720</convert>
				<convert type="ConditionalShowHide" />
		</widget>
		<widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/hd-ico.png" position="267,635" render="Pixmap" size="53,40" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
				<convert type="ServiceInfo">VideoWidth</convert>
				<convert type="ValueRange">721,1980</convert>
				<convert type="ConditionalShowHide" />
		</widget>
		<widget pixmap="/usr/lib/enigma2/python/Plugins/Extensions/Kodi/image/4k-uhd-ico.png" position="267,635" render="Pixmap" size="53,40" source="session.CurrentService" zPosition="60" alphatest="blend" scale="1">
				<convert type="ServiceInfo">VideoWidth</convert>
				<convert type="ValueRange">1921,4096</convert>
				<convert type="ConditionalShowHide" />
		</widget>
				</screen>"""

	RESUME_POPUP_ID = "kodiplayer_seekto"
	instance = None

	def __init__(self, session, playlistCallback, nextItemCallback, prevItemCallback, infoCallback, menuCallback):
		Screen.__init__(self, session)
		self.skinName = ['KodiVideoPlayer']
		statusScreen = self.session.instantiateDialog(StatusScreen)
		InfoBarBase.__init__(self, steal_current_service=True)
		SubsSupport.__init__(self, searchSupport=True, embeddedSupport=True)
		SubsSupportStatus.__init__(self)
		InfoBarSeek.__init__(self)
		InfoBarShowHide.__init__(self)
		InfoBarSubservicesSupport.__init__(self)
		InfoBarAspectChange.__init__(self)
		InfoBarAudioSelection.__init__(self)
		InfoBarNotifications.__init__(self)
		HelpableScreen.__init__(self)
		self.playlistCallback = playlistCallback
		self.nextItemCallback = nextItemCallback
		self.prevItemCallback = prevItemCallback
		self.infoCallback = infoCallback
		self.menuCallback = menuCallback
		self.statusScreen = statusScreen
		self.defaultImage = None
		self.postAspectChange.append(self.showAspectChanged)
		self.__timer = eTimer()
		self.__timer.callback.append(self.__seekToPosition)
		self.__image = None
		self.__position = None
		self.__firstStart = True
		self["genre"] = Label()

		# load meta info from json file provided by Kodi Enigma2Player
		try:
			meta = load(open(KODIEXTIN, "r"))
		except Exception as e:
			self.logger.error("failed to load meta from %s: %s", KODIEXTIN, str(e))
			meta = {}
		self.__image = Meta(meta).getImage()
		self["image"] = WebPixmap(self.__image, caching=True)

		self.genre = str(", ".join(Meta(meta).getGenre()))
		self.plot = Meta(meta).getPlot()

		self["genre"].setText(self.genre)

		# set title, image if provided
		self.title_ref = Meta(meta).getTitle()

		# set start position if provided
		self.setStartPosition(Meta(meta).getStartTime())

		self["directionActions"] = HelpableActionMap(self, "DirectionActions",
		{
			"downUp": (playlistCallback, _("Show playlist")),
			"upUp": (playlistCallback, _("Show playlist"))
		})

		self["okCancelActions"] = HelpableActionMap(self, "OkCancelActions",
		{
			"cancel": self.close
		})

		self["actions"] = HelpableActionMap(self, "KodiPlayerActions",
		{
			"menuPressed": (menuCallback, _("Show playback menu")),
			"infoPressed": (infoCallback, _("Show playback info")),
			"nextPressed": (nextItemCallback, _("Skip to next item in playlist")),
			"prevPressed": (prevItemCallback, _("Skip to previous item in playlist")),
			"seekFwdManual": self.keyr,
			"seekBackManual": self.keyl
		})

		self.eventTracker = ServiceEventTracker(self,
		{
			iPlayableService.evStart: self.__evStart,
		})

		try:
			if KodiVideoPlayer.instance:
				 raise AssertionError("class KodiVideoPlayer is a singleton class and just one instance of this class is allowed!")
		except:
			pass

		KodiVideoPlayer.instance = self

		self.onClose.append(boundFunction(self.session.deleteDialog, self.statusScreen))
		self.onClose.append(boundFunction(Notifications.RemovePopup, self.RESUME_POPUP_ID))
		self.onClose.append(self.__timer.stop)

	def keyr(self):
		try:
			if exists("/usr/lib/enigma2/python/Plugins/Extensions/TimeSleep/plugin.py") or exists("/usr/lib/enigma2/python/Plugins/Extensions/TimeSleep/plugin.so"):
				from Plugins.Extensions.TimeSleep.plugin import timesleep
				timesleep(self, True)
			else:
				InfoBarSeek.seekFwdManual(self)
		except:
			InfoBarSeek.seekFwdManual(self)

	def keyl(self):
		try:
			if exists("/usr/lib/enigma2/python/Plugins/Extensions/TimeSleep/plugin.py") or exists("/usr/lib/enigma2/python/Plugins/Extensions/TimeSleep/plugin.so"):
				from Plugins.Extensions.TimeSleep.plugin import timesleep
				timesleep(self, False)
			else:
				InfoBarSeek.seekBackManual(self)
		except:
			InfoBarSeek.seekBackManual(self)

	def __evStart(self):
		if self.__position and self.__firstStart:
			self.__firstStart = False
			Notifications.AddNotificationWithID(self.RESUME_POPUP_ID,
					MessageBox, _("Resuming playback"), timeout=0,
					type=MessageBox.TYPE_INFO, enable_input=True)
			self.__timer.start(500, True)

	def __seekToPosition(self):
		if getPlayPositionInSeconds(self.session) is None:
			self.__timer.start(500, True)
		else:
			Notifications.RemovePopup(self.RESUME_POPUP_ID)
			self.doSeek(self.__position)

	def setImage(self, image):
		self.__image = image

	def setStartPosition(self, positionInSeconds):
		try:
			self.__position = positionInSeconds * 90 * 1000
		except Exception:
			self.__position = None

	def stopService(self):
		self.session.nav.stopService()

	def playService(self, sref):
		if self.title_ref:
			sref.setName(self.title_ref)

		self.session.nav.playService(sref)

	def audioSelection(self):
		self.session.openWithCallback(self.audioSelected, MyAudioSelection, infobar=self)

	def subtitleSelection(self):
		from Screens.AudioSelection import SubtitleSelection
		self.session.open(SubtitleSelection, self)

	def showAspectChanged(self):
		self.statusScreen.setStatus(self.getAspectStr(), "#00ff00")

	def doEofInternal(self, playing):
		self.close()


class Meta(object):
	def __init__(self, meta):
		self.meta = meta

	def getTitle(self):
		title = u""
		vTag = self.meta.get('videoInfoTag')
		if vTag:
			if vTag.get('showtitle'):
				title = vTag["showtitle"]
				episode = vTag.get("episode", -1)
				try:
					episode = int(episode)
				except:
					episode = -1
				season = vTag.get("season", -1)
				try:
					season = int(season)
				except:
					season = -1
				if season > 0 and episode > 0:
					title += u" S%02dE%02d" % (season, episode)
				episodeTitle = vTag.get("title")
				if episodeTitle:
					title += u" - " + episodeTitle
			else:
				title = vTag.get("title") or vTag.get("originaltitle")
				year = vTag.get("year")
				if year and title:
					title += u" (" + str(year) + u")"
		if not title:
			title = self.meta.get("title")
		filename = self.getFilename()
		if not title and exists(str(filename) + ".spztxt"):
			f = open(str(filename) + ".spztxt", "r")
			tok = 0
			for line in f.readlines():
				idx = line.find("->")
				if idx != -1:
					if tok == 0:
						title = u'' + line[idx + 3:]
						break
			f.close()
		if not title:
			listItem = self.meta.get("listItem")
			if listItem:
				title = listItem.get("label")
		return title

	def getStartTime(self):
		startTime = 0
		playerOptions = self.meta.get("playerOptions")
		if playerOptions:
			startTime = playerOptions.get("startTime", 0)
		return startTime

	def getImage(self):
		image = None
		listItem = self.meta.get("listItem")
		if listItem:
			image = listItem.get("CacheThumb", "")
			fanart = listItem.get("Fanart", "")
			imageweb = ""
			if fanart:
				imageweb = fanart.get("thumb", "")

			if imageweb.startswith("http"):
				if not exists(image):
					image = imageweb
			else:
				filename = self.getFilename()
				if exists(str(filename) + ".png"):
					image = str(filename) + ".png"
				elif exists(str(filename) + ".gif"):
					image = str(filename) + ".gif"
				elif exists(str(filename) + ".jpg"):
					image = str(filename) + ".jpg"
		return image

	def getFilename(self):
		return self.meta.get("strPath")

	def getPlot(self):
		plot = u''
		vTag = self.meta.get('videoInfoTag')
		if vTag and vTag.get("plot"):
			plot = u'' + vTag.get("plot")

		filename = self.getFilename()
		if not plot and exists(str(filename) + ".spztxt"):
			f = open(str(filename) + ".spztxt", "r")
			tok = 0
			for line in f.readlines():
				idx = line.find("->")
				if idx != -1:
					if tok == 0:
						tok = 1
					elif tok == 1:
						plot = u'' + line[idx + 3:]
						break
			f.close()

		return plot

	def getGenre(self):
		genre = []
		vTag = self.meta.get('videoInfoTag')
		if vTag and vTag.get("genre"):
			genre = vTag.get("genre")

		filename = self.getFilename()
		if not genre and exists(str(filename) + ".spztxt"):
			f = open(str(filename) + ".spztxt", "r")
			for line in f.readlines():
				if line.split(":")[0] == 'Género':
					genrestr = u'' + line.split(":")[1][1:]
					genre = genrestr.split(" | ")
					break
			f.close()

		return genre


class VideoInfoView(Screen):
	if esHD():
		skin = """
		<screen position="center,center" size="1150,600" title="View Video Info" >
		   <widget name="image" position="15,150" size="300,400" alphatest="on" transparent="1"/>
		   <widget source="session.CurrentService" render="Label" position="20,20" size="1110,42" zPosition="1"  font="RegularHD;26" valign="center" halign="left" foregroundColor="#00ffa533" transparent="1">
			   <convert type="ServiceName">Name</convert>
		   </widget>
		   <widget name="genre" position="20,70" size="1110,35" zPosition="2" font="RegularHD;19" valign="center" halign="left"/>
		   <eLabel name="linea" position="20,110" size="1110,2" foregroundColor="#40444444" transparent="0" zPosition="20" backgroundColor="#30555555"/>
		   <widget source="description" position="330,150" size="800,400" font="RegularHD; 20" render="RunningTextSpa" options="movetype=swimming,startpoint=0,direction=top,steptime=100,repeat=0,always=0,oneshot=0,startdelay=15000,pause=500,backtime=5" noWrap="0"/>
		</screen>"""
	else:
		skin = """
		<screen position="center,center" size="766,400" title="View Video Info" >
		   <widget name="image" position="10,100" size="200,266" alphatest="on" transparent="1"/>
		   <widget source="session.CurrentService" render="Label" position="13,13" size="740,28" zPosition="1"  font="Regular;26" valign="center" halign="left" foregroundColor="#00ffa533" transparent="1">
			   <convert type="ServiceName">Name</convert>
		   </widget>
		   <widget name="genre" position="13,46" size="740,23" zPosition="2" font="Regular;19" valign="center" halign="left"/>
		   <eLabel name="linea" position="13,73" size="740,1" foregroundColor="#40444444" transparent="0" zPosition="20" backgroundColor="#30555555"/>
		   <widget source="description" position="220,100" size="533,266" font="Regular; 20" render="RunningTextSpa" options="movetype=swimming,startpoint=0,direction=top,steptime=100,repeat=0,always=0,oneshot=0,startdelay=15000,pause=500,backtime=5" noWrap="0"/>
		</screen>"""

	def __init__(self, session):
		self.skin = VideoInfoView.skin
		Screen.__init__(self, session)

		self["genre"] = Label()
		self["description"] = Label()
		# load meta info from json file provided by Kodi Enigma2Player
		try:
			meta = load(open(KODIEXTIN, "r"))
		except Exception as e:
			self.logger.error("failed to load meta from %s: %s", KODIEXTIN, str(e))
			meta = {}
		self.__image = Meta(meta).getImage()
		self["image"] = WebPixmap(self.__image, caching=True)

		self.genre = str(", ".join(Meta(meta).getGenre()))
		self.plot = str(Meta(meta).getPlot())

		self["genre"].setText(self.genre)
		self["description"].setText(self.plot)

		self["actions"] = ActionMap(["OkCancelActions"],
		{
				"cancel": self.close,
				"ok": self.close
		}, -1)


class E2KodiExtRequestHandler(KodiExtRequestHandler):

	def handle_request(self, opcode, status, data):
		self.server.messageOut.put((status, data))
		self.server.messagePump.send(opcode)
		return self.server.messageIn.get()


class E2KodiExtServer(UDSServer):
	def __init__(self):
		UDSServer.__init__(self, KODIEXT_SOCKET, E2KodiExtRequestHandler)
		self.kodiPlayer = None
		self.subtitles = []
		self.messageIn = Queue()
		self.messageOut = Queue()
		self.messagePump = ePythonMessagePump()
		self.messagePump.recv_msg.get().append(self.messageReceived)

	def shutdown(self):
		self.messagePump.stop()
		self.messagePump = None
		UDSServer.shutdown(self)

	def messageReceived(self, opcode):
		status, data = self.messageOut.get()
		if opcode == OP_CODE_EXIT:
			self.handleExitMessage(status, data)
		elif opcode == OP_CODE_PLAY:
			self.handlePlayMessage(status, data)
		elif opcode == OP_CODE_PLAY_STATUS:
			self.handlePlayStatusMessage(status, data)
		elif opcode == OP_CODE_PLAY_STOP:
			self.handlePlayStopMessage(status, data)
		elif opcode == OP_CODE_SWITCH_TO_ENIGMA2:
			self.handleSwitchToEnigma2Message(status, data)
		elif opcode == OP_CODE_SWITCH_TO_KODI:
			self.handleSwitchToKodiMessage(status, data)

	def handleExitMessage(self, status, data):
		self.messageIn.put((True, None))
		self.stopTimer = eTimer()
		self.stopTimer.callback.append(KODI_LAUNCHER.stop)
		self.stopTimer.start(500, True)

	def handlePlayStatusMessage(self, status, data):
		position = getPlayPositionInSeconds(SESSION)
		duration = getDurationInSeconds(SESSION)
		if position and duration:
			# decoder sometimes provides invalid position after seeking
			if position > duration:
				position = None
		statusMessage = {
			"duration": duration,
			"playing": self.kodiPlayer is not None,
			"position": position}
		self.messageIn.put((self.kodiPlayer is not None, dumps(statusMessage)))

	def handlePlayStopMessage(self, status, data):
		FBLock()
		RCLock()
		self.messageIn.put((True, None))

	def handleSwitchToEnigma2Message(self, status, data):
		self.messageIn.put((True, None))
		self.stopTimer = eTimer()
		self.stopTimer.callback.append(KODI_LAUNCHER.stop)
		self.stopTimer.start(500, True)

	def handleSwitchToKodiMessage(self, status, data):
		self.messageIn.put((True, None))

	def handlePlayMessage(self, status, data):
		if data is None:
			self.logger.error("handlePlayMessage: no data!")
			self.messageIn.put((False, None))
			return
		FBUnlock()
		RCUnlock()

		setaudio.switch(False, True)
		if MACHINEBRAND not in ('Vu+', 'Formuler'):
			setresolution.switch(False, True)
		# parse subtitles, play path and service type from data
		sType = 4097
		subtitles = []
		if isinstance(data, bytes):
			data = data.decode()
		dataSplit = data.strip().split("\n")
		if len(dataSplit) == 1:
			playPath = dataSplit[0]
		if len(dataSplit) == 2:
			playPath, subtitlesStr = dataSplit
			subtitles = subtitlesStr.split("|")
		elif len(dataSplit) >= 3:
			playPath, subtitlesStr, sTypeStr = dataSplit[:3]
			subtitles = subtitlesStr.split("|")
			try:
				sType = int(sTypeStr)
			except ValueError:
				self.logger.error("handlePlayMessage: '%s' is not a valid servicetype",
						sType)
		if playPath.startswith('http'):
			playPathSplit = playPath.split("|")
			if len(playPathSplit) > 1:
				playPath = playPathSplit[0] + "#" + playPathSplit[1]
		self.logger.debug("handlePlayMessage: playPath = %s", playPath)
		for idx, subtitlesPath in enumerate(subtitles):
			self.logger.debug("handlePlayMessage: subtitlesPath[%d] = %s", idx, subtitlesPath)

		# load meta info from json file provided by Kodi Enigma2Player
		try:
			meta = load(open(KODIEXTIN, "r"))
		except Exception as e:
			self.logger.error("failed to load meta from %s: %s", KODIEXTIN, str(e))
			meta = {}
		else:
			if meta.get("strPath") and meta["strPath"] not in data:
				self.logger.error("meta data for another filepath?")
				meta = {}

		# create Kodi player Screen
		noneFnc = lambda: None
		self.kodiPlayer = SESSION.openWithCallback(self.kodiPlayerExitCB, KodiVideoPlayer,
			noneFnc, noneFnc, noneFnc, self.infoview, noneFnc)

		# load subtitles
		if len(subtitles) > 0 and hasattr(self.kodiPlayer, "loadSubs"):
			# TODO allow to play all subtitles
			subtitlesPath = subtitles[0]
			self.kodiPlayer.loadSubs(subtitlesPath)

		# create service reference
		sref = eServiceReference(sType, 0, playPath)

		# set title, image if provided
		title = Meta(meta).getTitle()
		if not title:
			title = basename(playPath.split("#")[0])
		sref.setName(title)

		# set start position if provided
		#self.kodiPlayer.setStartPosition(Meta(meta).getStartTime())

		self.kodiPlayer.playService(sref)
		self.messageIn.put((True, None))

	def kodiPlayerExitCB(self, callback=None):
		setaudio.switch(True, True)
		if MACHINEBRAND not in ('Vu+', 'Formuler'):
			setresolution.switch(True, True)
		SESSION.nav.stopService()
		self.kodiPlayer = None
		self.subtitles = []

	def infoview(self):
		SESSION.open(VideoInfoView)


class KodiLauncher(Screen):
	skin = """<screen position="fill" backgroundColor="#FF000000" flags="wfNoBorder" title=" "></screen>"""

	def __init__(self, session):
		Screen.__init__(self, session)
		RCLock()
		self.previousService = self.session.nav.getCurrentlyPlayingServiceReference()
		self.session.nav.stopService()
		self.startupTimer = eTimer()
		self.startupTimer.timeout.get().append(self.startup)
		self.startupTimer.start(500, True)
		self.onClose.append(RCUnlock)

	def startup(self):
		def psCallback(data, retval, extraArgs):
			FBLock()
			kodiProc = None
			if isinstance(data, bytes):
				data = data.decode()
			procs = data.split("\n")
			if len(procs) > 0:
				for p in procs:
					if "kodi.bin" in p:
						if kodiProc is not None:
							print("[KodiLauncher] startup - there are more kodi processes running!")
							return self.stop()
						kodiProc = p.split()
			if kodiProc is not None:
				kodiPid = int(kodiProc[0])
				print("[KodiLauncher] startup: kodi is running, pid = %d , resuming..." % kodiPid)
				self.resumeKodi(kodiPid)
			else:
				print("[KodiLauncher] startup: kodi is not running, starting...")
				self.startKodi()

		self._checkConsole = Console()
		self._checkConsole.ePopen("ps | grep kodi.bin | grep -v grep", psCallback)

	def startKodi(self):
		self._startConsole = Console()
		self._startConsole.ePopen(KODIRUN_SCRIPT, kodiStopped)

	def resumeKodi(self, pid):
		self._resumeConsole = Console()
		self._resumeConsole.ePopen(KODIRESUME_SCRIPT % pid, kodiResumeStopped)

	def stop(self):
		FBUnlock()
		setaudio.switch()
		setresolution.switch()
		if self.previousService:
			self.session.nav.playService(self.previousService)
		try:
			if exists("/media/hdd/.kodi/"):
				system("rm -rf /media/hdd/kodi_crashlog*.log")
			else:
				system("rm -rf /tmp/kodi/kodi_crashlog*.log")
		except OSError:
			pass
		self.close()


def autoStart(reason, **kwargs):
	print("[KodiLauncher] autoStart - reason = %d" % reason)
	global SERVER_THREAD
	global SERVER
	if reason == 0:
		try:
			remove(KODIEXT_SOCKET)
		except OSError:
			pass
		SERVER = E2KodiExtServer()
		SERVER_THREAD = threading.Thread(target=SERVER.serve_forever)
		SERVER_THREAD.start()
	elif reason == 1:
		SERVER.shutdown()
		SERVER_THREAD.join()


def startLauncher(session, **kwargs):
	if config.kodi.standalone.value:
		session.open(TryQuitMainloop, retvalue=QUIT_KODI)
	else:
		setaudio.ReadData()
		# setaudio.switch(True)
		setresolution.ReadData()
		eRCInput.getInstance().unlock()
		global SESSION
		SESSION = session
		global KODI_LAUNCHER
		KODI_LAUNCHER = session.open(KodiLauncher)


def startMenuLauncher(menuid, **kwargs):
	if menuid == "mainmenu":
		return [("Kodi", startLauncher, "kodi", 1)]
	return []


class KodiExtSetup(Setup):
	def __init__(self, session):
		Setup.__init__(self, session, "Kodi", plugin="Extensions/Kodi")
		self["key_blue"] = StaticText(_("Start Kodi"))
		self["actions"] = HelpableActionMap(self, ["ColorActions"], {
			"blue": (self.startKodi, _("Start Kodi"))
		}, prio=-1, description=_("Kodi Actions"))

	def startKodi(self):
		self.close(True)


def startSetup(session, **kwargs):
	def kodiSetupCallback(result=None):
		if result and result is True:
			startLauncher(session)
	session.openWithCallback(kodiSetupCallback, KodiExtSetup)


def Plugins(**kwargs):
	screenwidth = getDesktop(0).size().width()
	kodiext = "kodiext_FHD.png" if screenwidth and screenwidth == 1920 else "kodiext_HD.png"
	l = [
		PluginDescriptor("Kodi", PluginDescriptor.WHERE_AUTOSTART, "Kodi Launcher", fnc=autoStart),
		PluginDescriptor("Kodi", PluginDescriptor.WHERE_PLUGINMENU, "Kodi Settings", icon=kodiext, fnc=startSetup)
	  ]
	if config.kodi.addToMainMenu.value:
		l.append(PluginDescriptor(name="Kodi", where=PluginDescriptor.WHERE_MENU, fnc=startMenuLauncher))
	if config.kodi.addToExtensionMenu.value:
		l.append(PluginDescriptor(name="Kodi", where=PluginDescriptor.WHERE_EXTENSIONSMENU, icon=kodiext, fnc=startLauncher))
	return l
