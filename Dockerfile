# Use official Python image
FROM python:3.10

# Set the working directory inside the container
WORKDIR /UniGrading

# Copy only requirements first (for efficient caching)
COPY ./UniGrading/requirements.txt /UniGrading/requirements.txt

# Upgrade pip and install dependencies
RUN pip install --upgrade pip
RUN pip install -r /UniGrading/requirements.txt

# Copy the rest of the project files
COPY ./UniGrading /UniGrading

# Expose port 8000 (important for Django)
EXPOSE 8000

# Start the Django server
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
