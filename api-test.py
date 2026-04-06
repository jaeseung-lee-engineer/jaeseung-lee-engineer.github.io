from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"message": "API is running"}

@app.get("/cases")
def get_cases():
    return [
        {"id": 1, "diagnosis": "Mixed Glioma (G3)"},
        {"id": 2, "diagnosis": "Oligodendroglioma"}
    ]
