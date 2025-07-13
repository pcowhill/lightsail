# lightsail

## For Developers

### Acquiring the Code

For the purposes of performing **development**, a git clone should be created.  For example:

`git clone https://github.com/pcowhill/lightsail.git`

For the purposes of performing **deployment**, you can simply get a copy of the code without using
git.  With wget, this would be:

`wget https://github.com/pcowhill/lightsail/archive/refs/heads/main.zip`

### Set-Up the Environment

First create a virtual environment.

`python -m venv venv`

Then, activate it.

Linux: `source venv/bin/activate`
Windows: `.\venv\Scripts\activate`

Then, install the necessary dependencies.

`python -m pip install --upgrade pip`
`pip install -r requirements.txt`

Note: If new packages are added in the course of development, "requirements.txt" can be updated.

`pip freeze > requirements.txt`

### Running the Code

To run the server, simply run:

`python ./main.py`

If you are connected via a terminal that might timeout (e.g a terminal connected via SSH), you might want to use `screen` to allow the server to continue running after the terminal has timed out.

First, see all current sessions that exist:

`screen -ls`

To connect to an existing session, run this.  This will attach you to that session.

`screen -r session_name`

To start a new session, run this.  This will also attach you to that session.

`screen -S session_name`

To detatch from an opened session without closing it, press:

`Ctrl + A, then D`

To kill a session while attached tp it, either run:

`exit`

Or use the shortcut:

`Ctrl + D`

To kill a session while not attached to it, run:

`screen -S session_name -X quit`