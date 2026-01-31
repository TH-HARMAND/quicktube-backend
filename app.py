from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import logging
from supabase import create_client
from datetime import datetime
import google.generativeai as genai
import yt_dlp
import re

app = Flask(__name__)
CORS(app, origins=os.getenv('ALLOWED_ORIGINS', '*').split(','))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_SERVICE_KEY')
)

genai.configure(api_key=os.getenv('GEMINI_API_KEY'))

def extract_video_id(url):
    """Extrait l'ID de la vidéo depuis l'URL YouTube"""
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

def get_transcript(video_url):
    """Extrait la transcription avec yt-dlp"""
    try:
        ydl_opts = {
            'skip_download': True,
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': ['fr', 'en'],
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            
            subtitles = info.get('subtitles', {})
            automatic_captions = info.get('automatic_captions', {})
            
            transcript_text = None
            language = None
            
            # Priorité : français manuel, anglais manuel, français auto, anglais auto
            for lang in ['fr', 'en']:
                if lang in subtitles:
                    subs = subtitles[lang]
                    if subs:
                        sub_url = subs[0]['url']
                        import requests
                        response = requests.get(sub_url)
                        transcript_text = response.text
                        language = lang
                        break
            
            if not transcript_text:
                for lang in ['fr', 'en']:
                    if lang in automatic_captions:
                        subs = automatic_captions[lang]
                        if subs:
                            sub_url = subs[0]['url']
                            import requests
                            response = requests.get(sub_url)
                            transcript_text = response.text
                            language = lang
                            break
            
            if not transcript_text:
                return None, "Aucune transcription disponible", None
            
            # Parser le VTT/SRT pour extraire le texte
            lines = transcript_text.split('\n')
            text_only = []
            for line in lines:
                line = line.strip()
                if line and not line.startswith('WEBVTT') and not '-->' in line and not line.isdigit():
                    text_only.append(line)
            
            final_text = ' '.join(text_only)
            
            metadata = {
                'title': info.get('title'),
                'channel': info.get('uploader'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
            }
            
            return final_text, metadata, language
            
    except Exception as e:
        logger.error(f"Erreur extraction: {str(e)}")
        return None, str(e), None

def generate_summary(transcript, metadata, style='structured'):
    """Génère un résumé avec Gemini"""
    
    prompts = {
        'structured': f"""Analyse cette transcription vidéo et crée un résumé structuré en français.

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
        
        'bullets': f"""Résume en 5-7 bullet points en français.

{transcript[:4000]}""",
        
        'paragraph': f"""Résumé en 1 paragraphe fluide en français (4-6 phrases).

{transcript[:4000]}"""
    }
    
    try:
        model = genai.GenerativeModel('gemini-pro')
        prompt = prompts.get(style, prompts['structured'])
        
        response = model.generate_content(prompt)
        return response.text, None
        
    except Exception as e:
        logger.error(f"Erreur Gemini: {str(e)}")
        return None, str(e)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'service': 'quicktube-backend',
        'version': 'yt-dlp-gemini',
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
        
        video_id = extract_video_id(video_url)
        if not video_id:
            return jsonify({'error': 'URL YouTube invalide'}), 400
        
        # Vérifier crédits
        user_response = supabase.table('profiles').select('credits_remaining, tier').eq('id', user_id).single().execute()
        
        if not user_response.data:
            return jsonify({'error': 'Utilisateur non trouvé'}), 404
        
        credits = user_response.data.get('credits_remaining', 0)
        
        if credits <= 0:
            return jsonify({'error': 'Crédits épuisés'}), 403
        
        # Extraire transcription
        logger.info(f"Extraction transcription pour {video_url}")
        transcript, metadata_or_error, language = get_transcript(video_url)
        
        if not transcript:
            return jsonify({'error': f'Extraction échouée: {metadata_or_error}'}), 500
        
        metadata = metadata_or_error
        
        # Générer résumé
        logger.info(f"Génération résumé Gemini")
        summary, error = generate_summary(transcript, metadata, style)
        
        if error:
            return jsonify({'error': f'Résumé échoué: {error}'}), 500
        
        # Sauvegarder
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
    required_env = ['SUPABASE_URL', 'SUPABASE_SERVICE_KEY', 'GEMINI_API_KEY']
    missing = [var for var in required_env if not os.getenv(var)]
    
    if missing:
        logger.error(f"Variables manquantes: {', '.join(missing)}")
        exit(1)
    
    port = int(os.getenv('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)