import os
from dash import Dash, dcc, html, Input, Output
app = Dash(__name__)  # prefix comes from DASH_* environment variables only
app.layout = html.Div([dcc.Input(id="i", value="a", type="text"), html.Div(id="o")])


@app.callback(Output("o", "children"), Input("i", "value"))
def cb(v):
    return "echo:" + (v or "")


app.run(host="127.0.0.1", port=int(os.environ["PORT"]), debug=False)
