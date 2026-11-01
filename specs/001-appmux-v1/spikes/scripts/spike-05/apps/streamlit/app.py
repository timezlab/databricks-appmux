import streamlit as st
st.title("hello streamlit")
x = st.text_input("name", "abc")
st.write("echo:" + x)
