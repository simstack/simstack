
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

In a typical installation Simstack II requires one Next-JS frontend, one
FastAPI backend, and one MongoDB database server. The backend is the only
component that connects to MongoDB. Runners are installed on the required
execution resources and communicate with the backend through the authenticated
runner API.

Runner processes do not update their own checkout or environment. They never
run Git or a dependency manager and never inspect a particular lock file or
user-specific dependency file. CI or the deployment supervisor must prepare a
validated, secret-free source snapshot and production environment, replace the
deployment, and restart the runner explicitly.
