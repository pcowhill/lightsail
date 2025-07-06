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