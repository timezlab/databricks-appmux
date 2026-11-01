import os
import gradio as gr
gr.Interface(fn=lambda x: "echo:" + x, inputs=gr.Textbox(label="in"), outputs=gr.Textbox(label="out"), flagging_mode="never").launch(server_name="127.0.0.1", server_port=int(os.environ["PORT"]), prevent_thread_lock=False)
