class Config(object):
    DEBUG=True

class Development(Config):
    STORAGE_PATH='/Volumes/PROJECTS/storage'
    UPLOAD_FOLDER = '/Volumes/PROJECTS/storage_staging'
    ALLOWED_EXTENSIONS = set(['txt', 'mp4', 'png', 'jpg', 'jpeg', 'gif', 'blend', 'zip'])
