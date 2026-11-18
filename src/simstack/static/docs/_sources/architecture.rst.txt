
Simstack Architecture
~~~~~~~~~~~~~~~~~~~~~

Simstack II was implemented using the following architecture:

.. figure:: resources/GUI\ Concept.webp
   :align: center
   :alt: GUI Concept Design

   Overview of the graphical user interface concept

Starting at the bottom right, then end user interacts with a web-based
graphical user interface (GUI) implemented using Next-JS. The GUI interacts
with a backend implemented using FastAPI, which in turn interacts with a
MongoDB database to persist data. The backend also interacts with remote
resources, so called runners, where the python code is actually executed. The graphical user interface
is decribed in more detail in :ref:`using_gui_section`.

A typical installation uses the Next.js frontend, the FastAPI backend, MongoDB
and S3-compatible object storage. Each user's MongoDB database holds scientific
records, task history and references to objects in S3. The bucket holds published
files, large table payloads and dataset membership pages. Runners on the user's
resources transfer files using signed URLs issued by Server.
Server publishes approved repository revisions and requests preparation of their
pinned runtimes through the Runner API. Running tasks retain their original
revision and environment when a new revision is prepared.

