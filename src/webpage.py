"""Embedded browser page used to log into Mixamo and read its access token."""

# Third-party modules
from PySide6 import QtCore
from PySide6 import QtWebEngineCore


# QWebEnginePage message levels, mapped onto the log levels the UI uses.
LEVELS = {
    QtWebEngineCore.QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: "debug",
    QtWebEngineCore.QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: "warning",
    QtWebEngineCore.QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: "error",
}

TOKEN_MARKER = "MIXAMO_DL_ACCESS_TOKEN:"

# Read the token straight out of localStorage and print it with a marker only
# this application looks for.
TOKEN_SCRIPT = f"""
(function () {{
    var token = window.localStorage.getItem('access_token');
    console.log('{TOKEN_MARKER}' + (token || ''));
}})();
"""


def extract_token(message):
    """Pull an access token out of a console message, if it carries one.

    Kept as a plain function so the parsing can be tested without spinning up
    a browser process.

    :param message: Text printed to the JavaScript console
    :type message: str

    :return: The token, an empty string when the page reported none, or None
        when the message is an ordinary console line
    :rtype: str or None
    """
    text = message or ""

    if TOKEN_MARKER in text:
        return text.split(TOKEN_MARKER, 1)[1].strip()

    # Legacy marker, kept so an older cached page still works after an update.
    if "ACCESS TOKEN" in text:
        return text.split(":")[-1].strip()

    return None


class CustomWebPage(QtWebEngineCore.QWebEnginePage):
    """QWebEnginePage that captures data from the JavaScript console.

    This is how we read values that only exist inside the browser, such as
    the 'access_token' Mixamo keeps in localStorage. That token is then sent
    as an 'Authorization' header when calling the Mixamo API.

    Console messages that are not the token are forwarded as well, so the
    application log can show what the embedded browser is complaining about
    instead of swallowing it.
    """

    retrieved_token = QtCore.Signal(str)
    console_message = QtCore.Signal(str, str)

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceID):
        """Decide what to do with a console message.

        :param level: Severity level a JavaScript console message can have
        :type level: QWebEnginePage.JavaScriptConsoleMessageLevel

        :param message: Message printed to the console
        :type message: str

        :param lineNumber: Line number where the message was printed
        :type lineNumber: int

        :param sourceID: Source ID
        :type sourceID: str
        """
        token = extract_token(message)

        if token is not None:
            # An empty value means the user is not logged in yet; emit it
            # anyway so the caller can tell "no token" from "never answered".
            self.retrieved_token.emit(token)
            return

        self.console_message.emit(LEVELS.get(level, "debug"), message or "")

    def request_token(self):
        """Ask the page for the current access token.

        The answer arrives asynchronously through :attr:`retrieved_token`.
        """
        self.runJavaScript(TOKEN_SCRIPT)
