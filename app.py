from flask import Flask, request, jsonify
from flask_cors import CORS
import os
from openai import OpenAI
import logging
from supabase import create_client
from datetime import datetime
import requests

app = Flask(__name__)
CORS(app, origins=os.getenv('ALLOWED_ORIGINS', '*').split(','))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_SERVICE_KEY')
)

YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')

def extract_video_id(url):
    """Extrait l'ID de la vidéo depuis l'URL YouTube"""
    import re
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/)([^&\n?#]+)',
        r'youtube\.com\/embed\/([^&\n?#]+)',
        r'youtube\.com\/v\/([^&\n?#]+)'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def get_video_info(video_id):
    """Récupère les infos de la vidéo via l'API YouTube"""
    try:
        url = f"https://www.googleapis.com/youtube/v3/videos"
        params = {
            'part': 'snippet,contentDetails,statistics',
            'id': video_id,
            'key': YOUTUBE_API_KEY
        }
        
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        
        if not data.get('items'):
            return None, "Vidéo non trouvée"
        
        video = data['items'][0]
        snippet = video['snippet']
        content = video['contentDetails']
        stats = video['statistics']
        
        # Parser la durée ISO 8601 (PT1H2M10S -> secondes)
        import re
        duration_str = content['duration']
        duration_match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
        hours = int(duration_match.group(1) or 0)
        minutes = int(duration_match.group(2) or 0)
        seconds = int(duration_match.group(3) or 0)
        duration_seconds = hours * 3600 + minutes * 60 + seconds
        
        return {
            'title': snippet['title'],
            'channel': snippet['channelTitle'],
            'duration': duration_seconds,
            'thumbnail': snippet['thumbnails']['high']['url'],
            'view_count': int(stats.get('viewCount', 0)),
            'upload_date': snippet['publishedAt']
        }, None
        
    except Exception as e:
        logger.error(f"Erreur récupération infos vidéo: {str(e)}")
        return None, str(e)

def get_transcript(video_id):
    """Récupère la transcription via l'API YouTube"""
    try:
        # Étape 1 : Récupérer la liste des sous-titres
        url = f"https://www.googleapis.com/youtube/v3/captions"
        params = {
            'part': 'snippet',
            'videoId': video_id,
            'key': YOUTUBE_API_KEY
        }
        
        response = requests.get(url, params=params)
        response.raise_for_status()
        data = response.json()
        
        if not data.get('items'):
            return None, "Aucun sous-titre disponible", None
        
        # Prioriser français puis anglais
        caption_id = None
        language = None
        
        for item in data['items']:
            lang = item['snippet']['language']
            if lang == 'fr':
                caption_id = item['id']
                language = 'fr'
                break
            elif lang == 'en' and not caption_id:
                caption_id = item['id']
                language = 'en'
        
        if not caption_id:
            # Prendre le premier disponible
            caption_id = data['items'][0]['id']
            language = data['items'][0]['snippet']['language']
        
        # Étape 2 : Télécharger le sous-titre
        # Note : L'API YouTube Data v3 ne permet PAS de télécharger directement les sous-titres
        # Il faut utiliser youtube-transcript-api (bibliothèque Python) à la place
        
        from youtube_transcript_api import YouTubeTranscriptApi
        
        try:
            # Essayer français puis anglais
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            
            transcript = None
            try:
                transcript = transcript_list.find_transcript(['fr'])
                language = 'fr'
            except:
                try:
                    transcript = transcript_list.find_transcript(['en'])
                    language = 'en'
                except:
                    transcript = transcript_list.find_generated_transcript(['fr', 'en'])
                    language = transcript.language_code
            
            transcript_data = transcript.fetch()
            transcript_text = ' '.join([entry['text'] for entry in transcript_data])
            
            return transcript_text, None, language
            
        except Exception as e:
            logger.error(f"Erreur téléchargement transcription: {str(e)}")
            return None, str(e), None
        
    except Exception as e:
        logger.error(f"Erreur API YouTube: {str(e)}")
        return None, str(e), None

def generate_summary(transcript, metadata, style='structured'):
    """Génère un résumé avec OpenAI GPT-4"""
    
    prompts = {
        'structured': f"""Analyse cette transcription et crée un résumé structuré en français.

Titre: {metadata.get('title')}

TRANSCRIPTION:
{transcript[:4000]}

FORMAT:
## 📝 Résumé Principal
[2-3 phrases]

## 🎯 Points Clés
- Point 1
- Point 2
- Point 3

## 💡 Idées Principales
[Développement]

## 🔑 Conclusion
[Takeaway]""",
        
        'bullets': f"""Résume en bullet points en français.

Titre: {metadata.get('title')}

TRANSCRIPTION:
{transcript[:4000]}

5-7 points clés.""",
        
        'paragraph': f"""Résumé en paragraphe fluide en français.

Titre: {metadata.get('title')}

TRANSCRIPTION:
{transcript[:4000]}

1 paragraphe de 4-6 phrases."""
    }
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "Tu es expert en résumé de vidéos."},
                {"role": "user", "content": prompts.get(style, prompts['structured'])}
            ],
            temperature=0.7,
            max_tokens=1000
        )
        return response.choices[0].message.content, None
    except Exception as e:
        logger.error(f"Erreur résumé: {str(e)}")
        return None, str(e)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'service': 'quicktube-backend',
        'version': 'youtube-api',
        'timestamp': datetime.utcnow().isoformat()
    })

@app.route('/api/process-video', methods=['POST'])
def process_video():
    try:
        data = request.get_json()
        video_url = data.get('video_url')
        user_id = data.get('user_id')
        style = data.get('style', 'structured')
        
        if not video_url or not user_id:
            return jsonify({'error': 'video_url et user_id requis'}), 400
        
        # Extraire l'ID vidéo
        video_id = extract_video_id(video_url)
        if not video_id:
            return jsonify({'error': 'URL YouTube invalide'}), 400
        
        # Vérifier les crédits
        user_response = supabase.table('profiles').select('credits_remaining, tier').eq('id', user_id).single().execute()
        
        if not user_response.data:
            return jsonify({'error': 'Utilisateur non trouvé'}), 404
        
        credits = user_response.data.get('credits_remaining', 0)
        
        if credits <= 0:
            return jsonify({'error': 'Crédits épuisés'}), 403
        
        # Récupérer les infos de la vidéo
        logger.info(f"Récupération infos pour video_id={video_id}")
        metadata, error = get_video_info(video_id)
        
        if error:
            return jsonify({'error': f'Infos vidéo échouées: {error}'}), 500
        
        # Récupérer la transcription
        logger.info(f"Récupération transcription pour video_id={video_id}")
        transcript, error, language = get_transcript(video_id)
        
        if error:
            return jsonify({'error': f'Transcription échouée: {error}'}), 500
        
        # Générer le résumé
        logger.info(f"Génération résumé style={style}")
        summary, error = generate_summary(transcript, metadata, style)
        
        if error:
            return jsonify({'error': f'Résumé échoué: {error}'}), 500
        
        # Sauvegarder en base
        summary_record = {
            'user_id': user_id,
            'video_url': video_url,
            'video_title': metadata.get('title'),
            'video_duration': metadata.get('duration'),
            'thumbnail_url': metadata.get('thumbnail'),
            'channel_name': metadata.get('channel'),
            'transcript': transcript,
            'summary': summary,
            'language': language,
            'style': style,
            'created_at': datetime.utcnow().isoformat()
        }
        
        insert_response = supabase.table('summaries').insert(summary_record).execute()
        
        # Décrémenter crédits
        supabase.table('profiles').update({
            'credits_remaining': credits - 1
        }).eq('id', user_id).execute()
        
        logger.info(f"Succès - ID: {insert_response.data[0]['id']}")
        
        return jsonify({
            'success': True,
            'summary_id': insert_response.data[0]['id'],
            'summary': summary,
            'metadata': metadata,
            'credits_remaining': credits - 1
        }), 200
        
    except Exception as e:
        logger.error(f"Erreur: {str(e)}")
        return jsonify({'error': 'Erreur serveur'}), 500

if __name__ == '__main__':
    required_env = ['OPENAI_API_KEY', 'SUPABASE_URL', 'SUPABASE_SERVICE_KEY', 'YOUTUBE_API_KEY']
    missing = [var for var in required_env if not os.getenv(var)]
    
    if missing:
        logger.error(f"Variables manquantes: {', '.join(missing)}")
        exit(1)
    
    port = int(os.getenv('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
```

---

## 📦 ÉTAPE 3 : Mettre à jour requirements.txt
```
flask==3.0.0
flask-cors==4.0.0
openai==1.54.3
supabase==2.7.4
python-dotenv==1.0.0
gunicorn==21.2.0
requests>=2.32.2
youtube-transcript-api==0.6.2