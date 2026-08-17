from gary_api import GaryAPI
from flask import Flask, request
from flask_cors import CORS, cross_origin
from config import base_conf

app = Flask(__name__)
cors = CORS(app, resources={r"/api/*": {"Access-Control-Allow-Origin": "*"}})


@app.route("/", methods=["GET", "POST"])
@cross_origin(app)
def home():
    return "<h1>Here's where the main web app can live</h1>"


@cross_origin(app)
@app.route("/api/commands", methods=["GET", "POST"])
def commands():
    if request.method == "POST":
        GaryAPI().set_serial(base_conf["usb_port"]).send_commands(request.get_json())
        return ""


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
    print("\n Gary is listening... \n")
