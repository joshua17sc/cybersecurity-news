#!/usr/bin/env python

import os
import datetime
import markdown2
import boto3
import requests
import logging
import json
import psutil
from pydub import AudioSegment
from bs4 import BeautifulSoup
import re
from openai import OpenAI
from datetime import datetime, timedelta, timezone
import subprocess

# Configuration (using os.path.join for platform independence)
PODBEAN_API_BASE_URL = 'https://api.podbean.com'
PODBEAN_UPLOAD_AUTHORIZE_URL = f"{PODBEAN_API_BASE_URL}/v1/files/uploadAuthorize"
PODBEAN_PUBLISH_URL = f"{PODBEAN_API_BASE_URL}/v1/episodes"
FILE_PATH = os.path.join('/tmp', 'audio_files')  # Store audio files in a subdirectory
LOG_FILE = 'resource_usage.log'
USER_AGENT = 'Cybersecurity News'
MAX_CHARS_PER_REQUEST = 10000
OVERLAP_CHARS = 500
MAX_TEXT_LENGTH = 1000
BITRATE = '64k'

# Load environment variables
AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION')
NEWS_API_KEY = os.getenv('NEWS_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PODBEAN_TOKEN_FILE = os.getenv('PODBEAN_TOKEN_FILE')  # Path to the Podbean token file

# Initialize OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Logging Setup
def set_logging_level(level):
    logging.basicConfig(filename=LOG_FILE, level=level, format='%(asctime)s - %(levelname)s - %(message)s')

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Resource Usage Logging
def log_resource_usage():
    process = psutil.Process(os.getpid())
    logger.info(f"Memory usage: {process.memory_info().rss / 1024 ** 2:.2f} MB")
    logger.info(f"CPU usage: {process.cpu_percent(interval=1.0)}%")

# Markdown Parsing
def clean_markdown(content):
    logger.info("Cleaning markdown content")
    # Remove the first line containing the date
    content = re.sub(r'^---.*?---\n', '', content, flags=re.DOTALL)
    return content

def parse_markdown(content):
    logger.info("Parsing markdown content")
    try:
        html_content = markdown2.markdown(content)
        articles = html_content.split('<h2>')[1:]  # Assuming each article starts with <h2> header
        parsed_articles = [BeautifulSoup('<h2>' + article, 'html.parser') for article in articles]
        return parsed_articles
    except Exception as e:
        logger.error(f"Error parsing markdown content: {e}")
        raise

def create_podcast_script(articles, today_date):
    logger.info("Creating podcast script")
    intro = f"This is your daily cybersecurity news for {today_date}."
    outro = f"This has been your cybersecurity news for {today_date}. Tune in tomorrow and share with your friends and colleagues."

    script = [intro]
    for i, article in enumerate(articles):
        if i == 0:
            transition = "Our first article for today..."
        elif i == len(articles) - 1:
            transition = "Our final article for today..."
        else:
            transition = "This next article..."

        script.append(transition)
        
        article_text = article.get_text()
        article_lines = article_text.split('\n')
        filtered_lines = [line for line in article_lines if "Read more" not in line]
        script.append("\n".join(filtered_lines))
    
    script.append(outro)

    full_script = "\n".join(script)
    logger.debug(f"Generated Script: {full_script}")
    return full_script

def split_text(text, max_length):
    chunks = []
    while len(text) > max_length:
        split_index = text[:max_length].rfind('. ')
        if split_index == -1:
            split_index = max_length
        chunks.append(text[:split_index + 1])
        text = text[split_index + 1:]
    chunks.append(text)
    return chunks

# Speech Synthesis
def synthesize_speech(script_text, output_path):
    logger.info("Synthesizing speech using AWS Polly")
    polly_client = boto3.client('polly')
    chunks = split_text(script_text, MAX_TEXT_LENGTH)
    audio_segments = []

    try:
        for i, chunk in enumerate(chunks):
            logger.info(f"Synthesizing chunk {i+1}/{len(chunks)}")
            logger.debug(f"Text Chunk: {chunk}")
            response = polly_client.synthesize_speech(
                Text=chunk,
                OutputFormat='mp3',
                TextType='text',
                VoiceId='Ruth',  # Using Ruth voice for newscasting
                Engine='neural'
            )
            temp_audio_path = f'/tmp/temp_audio_{i}.mp3'
            with open(temp_audio_path, 'wb') as file:
                file.write(response['AudioStream'].read())
            audio_segments.append(AudioSegment.from_mp3(temp_audio_path))
            os.remove(temp_audio_path)  # Delete temporary file to free up memory

        combined_audio = sum(audio_segments)
        compressed_audio_path = output_path.replace(".mp3", "_compressed.mp3")
        combined_audio.export(compressed_audio_path, format='mp3', bitrate=BITRATE)
        logger.info(f"Compressed audio file saved to {compressed_audio_path}")
        log_resource_usage()  # Log resource usage after processing
        return compressed_audio_path
    except Exception as e:
        logger.error(f"Error in speech synthesis: {e}")
        # Return an empty string or None to indicate failure
        return None
    
# Podbean Interactions
def read_podbean_token(file_path):
    logger.info(f"Reading Podbean token from {file_path}")
    try:
        with open(file_path, 'r') as file:
            return json.load(file)['access_token']
    except Exception as e:
        logger.error(f"Error reading Podbean token: {e}")
        raise

def get_upload_authorization(token, filename, filesize, content_type='audio/mpeg'):
    try:
        logger.info("Getting upload authorization from Podbean")
        params = {
            'access_token': token,
            'filename': filename,
            'filesize': filesize,
            'content_type': content_type
        }
        response = requests.get(PODBEAN_UPLOAD_AUTHORIZE_URL, params=params)
        logger.info(f"Upload authorization response status: {response.status_code}")
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error getting upload authorization: {e}")
        raise

def upload_to_podbean(upload_url, audio_file_path):
    logger.info(f"Uploading audio file to Podbean: {audio_file_path}")
    try:
        with open(audio_file_path, 'rb') as file:
            response = requests.put(upload_url, data=file)
            logger.info(f"Podbean upload response status: {response.status_code}")
            response.raise_for_status()
            logger.info("Upload successful")
    except Exception as e:
        logger.error(f"Error uploading to Podbean: {e}")
        raise

def publish_episode(token, title, content, media_key):
    try:
        logger.info("Publishing episode on Podbean")
        data = {
            'access_token': token,
            'title': title,
            'content': content,
            'status': 'publish',
            'type': 'public',
            'media_key': media_key
        }
        response = requests.post(PODBEAN_PUBLISH_URL, data=data)
        logger.info(f"Episode publish response status: {response.status_code}")
        response.raise_for_status()
        logger.info("Episode published successfully")
        return response.json()
    except Exception as e:
        logger.error(f"Error publishing episode: {e}")
        raise

def create_html_description(parsed_articles):
    description = ""
    for parsed_article in parsed_articles:
        for element in parsed_article:
            if element.name == "h2":
                link = element.find("a")
                if link:
                    # Escape quotes in the href attribute only
                    link["href"] = link["href"].replace('"', "&quot;")  
                    description += str(element) 
            elif element.name in ["p", "ul"]:
                description += str(element)  # Keep the element as is
    return description

# News Fetching (from new_blog_post.txt)
def fetch_top_articles():
    logging.info("Fetching top articles...")
    try:
        url = 'https://newsapi.org/v2/everything'
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')
        params = {
            'q': 'cybersecurity',
            'from': yesterday,
            'to': yesterday,
            'sortBy': 'popularity',
            'pageSize': 20,
            'apiKey': NEWS_API_KEY,
            'language': 'en'  # Ensures articles are in English
        }
        response = requests.get(url, params=params)
        response.raise_for_status()
        articles = response.json().get('articles', [])
        logging.info(f"Fetched {len(articles)} articles.")
        return articles
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching articles: {e}")
        return []

def scrape_article_content(url):
    logging.info(f"Scraping content from {url}...")
    try:
        response = requests.get(url)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        paragraphs = soup.find_all('p')
        full_text = ' '.join([para.text for para in paragraphs])
        logging.info(f"Scraped content from {url}")
        return full_text
    except requests.exceptions.RequestException as e:
        logging.error(f"Error scraping article content from {url}: {e}")
        return ""

def summarize_article(article_text):
    logging.info("Summarizing article...")
    try:
        stream = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "user",
                    "content": f"As a cybersecurity professional that is trying to help other cyber professionals understand the latest cybersecurity news, summarize this article, focusing on the most important and relevant point when an article covers several topics, but without pointing it out as the most important and relevant:\n\n{article_text}"
                }
            ],
            stream=True,
        )
        summary = ""
        for chunk in stream:
            if chunk.choices[0].delta.content is not None:
                summary += chunk.choices[0].delta.content
        logging.info("Article summarized.")
        return summary
    except Exception as e:
        logging.error(f"Error summarizing article: {e}")
        return "Summary unavailable due to an error."

def generate_new_title(summary_text):
    logging.info("Generating new title...")
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "user",
                    "content": f"Generate a concise and compelling title for the following summary:\n\n{summary_text}"
                }
            ],
            stream=True,
        )
        new_title = ""
        for chunk in response:
            if chunk.choices[0].delta.content is not None:
                new_title += chunk.choices[0].delta.content
        logging.info("New title generated.")
        return new_title.strip()
    except Exception as e:
        logging.error(f"Error generating new title: {e}")
        return "Title unavailable due to an error."

def process_article(article):
    logging.info(f"Processing article: {article['title']}")
    full_text = scrape_article_content(article['url'])
    if full_text:
        summary = summarize_article(full_text)
        new_title = generate_new_title(summary)
        return {
            'original_title': article['title'],
            'new_title': new_title,
            'url': article['url'],
            'summary': summary
        }
    return None

def filter_relevant_articles(articles):
    logging.info("Filtering relevant articles...")
    with ThreadPoolExecutor() as executor:
        processed_articles = list(executor.map(process_article, articles))
    
    summarized_articles = [article for article in processed_articles if article is not None]

    try:
        combined_summaries = "\n\n".join([f"Title: {article['new_title']}\nSummary: {article['summary']}" for article in summarized_articles])
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "user",
                    "content": f"Select the top 8 most relevant articles for a cybersecurity professional from the following summaries, including removing those that cover multiple news events in a single article:\n\n{combined_summaries}"
                }
            ],
            stream=True,
        )
        relevant_titles = ""
        for chunk in response:
            if chunk.choices[0].delta.content is not None:
                relevant_titles += chunk.choices[0].delta.content

        relevant_articles = []
        for article in summarized_articles:
            if article['new_title'] in relevant_titles:
                relevant_articles.append(article)
                if len(relevant_articles) == 8:
                    break

        logging.info(f"Filtered down to {len(relevant_articles)} relevant articles.")
        return relevant_articles
    except Exception as e:
        logging.error(f"Error filtering relevant articles: {e}")
        return []

# Markdown Creation and GitHub Push
def create_markdown_content(summaries, today_date):
    """Creates markdown content for the blog post in memory."""
    markdown_content = f"---\ntitle: Cybersecurity News for {today_date}\ndate: {today_date}\n---\n\n"
    for article in summaries:
        markdown_content += f"## {article['new_title']}\n"
        markdown_content += f"[Read more]({article['url']})\n\n"
        markdown_content += f"{article['summary']}\n\n"
    return markdown_content

#def push_to_github(markdown_content):
#    """Pushes the markdown content directly to GitHub using the API."""
#    try:
#        g = Github(GITHUB_TOKEN)
#        repo = g.get_repo(GITHUB_REPO)
#        contents = repo.get_contents("_posts/cybersecurity-news.md") # Get the contents of the file.
#        repo.update_file(contents.path, "Updating blog post", markdown_content, contents.sha)  # Update the file.
#        logger.info("Blog post updated on GitHub.")
#    except Exception as e:
#        logger.error(f"Failed to update GitHub blog post: {e}")

# Main Function
# Main Function (Corrected)
def main():
    compressed_audio_path = None
    output_audio_path = None
    try:
        today_date = datetime.date.today().strftime('%Y-%m-%d')
        day_month_format = datetime.date.today().strftime('%-d %B %Y')
        file_name = f"cybersecurity-news-{today_date}.md"
        file_path = os.path.join("..", "_posts", file_name)

        # Fetch and filter articles (from new_blog_post.txt)
        articles = fetch_top_articles()
        relevant_articles = filter_relevant_articles(articles)

        # Create markdown content
        markdown_content = create_markdown_content(relevant_articles, today_date)
        logger.info(f"Markdown file written to: {file_path}")

        # Parse markdown and create podcast script (from podcast_upload.txt)
        parsed_articles = parse_markdown(markdown_content)
        script_text = create_podcast_script(parsed_articles, today_date)

        # Synthesize speech and get audio file path (from podcast_upload.txt)
        output_audio_path = os.path.join(FILE_PATH, f'daily_cybersecurity_news_{today_date}.mp3')
        os.makedirs(FILE_PATH, exist_ok=True)  # Create audio_files directory if it doesn't exist
        compressed_audio_path = synthesize_speech(script_text, output_audio_path)

        if compressed_audio_path is None:  # Check if synthesis failed
            raise Exception("Speech synthesis failed. Exiting script.")
        
        # Podbean Upload and Publish
        podbean_token = read_podbean_token()
        filename = os.path.basename(compressed_audio_path)
        filesize = os.path.getsize(compressed_audio_path)
        auth_data = get_upload_authorization(podbean_token, filename, filesize)
        upload_to_podbean(auth_data['upload_url'], compressed_audio_path)
        episode_data = publish_episode(podbean_token, f"Cybersecurity News for {day_month_format}", create_html_description(parsed_articles), auth_data['media_url'])
        logger.info(f"Episode published: {episode_data['url']}")

        # Write the markdown content to the _posts directory
        with open(file_path, "w") as file:
            file.write(markdown_content)

    except Exception as e:
        logger.error(f"An error occurred: {e}")
    finally:
        # Clean up temporary audio files (only if they exist)
        for file_path in [compressed_audio_path, output_audio_path]:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)

if __name__ == "__main__":
    set_logging_level(logging.DEBUG)  # Set logging level to DEBUG
    main()

