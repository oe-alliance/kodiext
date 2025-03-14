from logging import basicConfig, getLogger, ERROR
from os import getenv
from socketserver import BaseRequestHandler, UnixStreamServer
from struct import calcsize, pack, unpack

try:
	loglevel = int(getenv("E2KODI_DEBUG_LVL", ERROR))
except Exception:
	loglevel = ERROR
print("E2KODI_DEBUG_LVL = ", loglevel)

basicConfig(level=loglevel, format='%(name)s: %(message)s',)


class KodiExtRequestHandler(BaseRequestHandler):

	def __init__(self, request, client_address, server):
		self.logger = getLogger('KodiExtRequestHandler')
		BaseRequestHandler.__init__(self, request, client_address, server)

	def handle(self):
		hlen = calcsize('ibi')
		header = self.request.recv(hlen)
		opcode, status, datalen = unpack('ibi', header)
		if datalen > 0:
			data = self.request.recv(datalen)
		else:
			data = None
		self.logger.debug('recv()-> opcode = %d, status = %d, data = %s', opcode, status, str(data))
		status, data = self.handle_request(opcode, status, data)
		if data is not None:
			datalen = len(data)
		else:
			datalen = 0
		self.logger.debug('send()-> opcode = %d, status = %d, data = %s', opcode, status, str(data))
		header = pack('ibi', opcode, status, datalen)
		self.request.send(header)
		if datalen > 0:
			self.request.send(bytes(data, 'utf-8', errors='ignore'))

	def handle_request(self, opcode, status, data):
		return True, None


class UDSServer(UnixStreamServer):

	def __init__(self, server_address, handler_class=KodiExtRequestHandler):
		self.logger = getLogger('UDSServer')
		self.allow_reuse_address = True
		UnixStreamServer.__init__(self, server_address, handler_class)
