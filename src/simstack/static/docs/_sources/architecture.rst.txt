
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

In a typical installation Simstack II requrires only one instance of the
Next-JS frontend, one instance of the FastAPI backend and one instance of
the MongoDB database server. Each user has his/her own database in the
MongoDB server. The runners are installed for each user on the required remote resources and automatically update themselves
from one or several GIT repositories, which may be different for different users.

