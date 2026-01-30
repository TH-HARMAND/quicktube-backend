from flask import Flask, request, jsonify
from flask_cors import CORS
import yt_dlp
import os
from openai import OpenAI
import logging
from supabase import create_client
from datetime import datetime

app = Flask(__name__)
CORS(app, origins=os.getenv('ALLOWED_ORIGINS', '*').split(','))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_SERVICE_KEY')
)

YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'writesubtitles': True,
    'writeautomaticsub': True,
    'subtitleslangs': ['fr', 'en'],
    'skip_download': True,
}

def extract_transcript(video_url):
    try:
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(video_url, download=False)
            
            subtitles = info.get('subtitles', {})
            automatic_captions = info.get('automatic_captions', {})
            
            for lang in ['fr', 'en']:
                if lang in subtitles:
                    captions = subtitles[lang]
                    break
                elif lang in automatic_captions:
                    captions = automatic_captions[lang]
                    break
            else:
                return None, "Aucune transcription disponible"
            
            transcript_text = ""
            for caption in captions:
                if caption.get('ext') == 'json3':
                    caption_url = caption.get('url')
                    if caption_url:
                        import requests
                        response = requests.get(caption_url)
                        caption_data = response.json()
                        
                        for event in caption_data.get('events', []):
                            for seg in event.get('segs', []):
                                transcript_text += seg.get('utf8', '') + " "
                        break
            
            metadata = {
                'title': info.get('title'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
                'channel': info.get('uploader'),
                'view_count': info.get('view_count'),
                'upload_date': info.get('upload_date')
            }
            
            return {
                'transcript': transcript_text.strip(),
                'metadata': metadata,
                'language': lang
            }, None
            
    except Exception as e:
        logger.error(f"Erreur extraction: {str(e)}")
        return None, str(e)

def generate_summary(transcript, metadata, style='structured'):
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

5-7 points clés numérotés.""",
        
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
        
        user_response = supabase.table('profiles').select('credits_remaining, tier').eq('id', user_id).single().execute()
        
        if not user_response.data:
            return jsonify({'error': 'Utilisateur non trouvé'}), 404
        
        credits = user_response.data.get('credits_remaining', 0)
        
        if credits <= 0:
            return jsonify({'error': 'Crédits épuisés'}), 403
        
        logger.info(f"Extraction pour {video_url}")
        transcript_data, error = extract_transcript(video_url)
        
        if error:
            return jsonify({'error': f'Extraction échouée: {error}'}), 500
        
        logger.info(f"Génération résumé style={style}")
        summary, error = generate_summary(
            transcript_data['transcript'],
            transcript_data['metadata'],
            style
        )
        
        if error:
            return jsonify({'error': f'Résumé échoué: {error}'}), 500
        
        summary_record = {
            'user_id': user_id,
            'video_url': video_url,
            'video_title': transcript_data['metadata'].get('title'),
            'video_duration': transcript_data['metadata'].get('duration'),
            'thumbnail_url': transcript_data['metadata'].get('thumbnail'),
            'channel_name': transcript_data['metadata'].get('channel'),
            'transcript': transcript_data['transcript'],
            'summary': summary,
            'language': transcript_data['language'],
            'style': style,
            'created_at': datetime.utcnow().isoformat()
        }
        
        insert_response = supabase.table('summaries').insert(summary_record).execute()
        
        supabase.table('profiles').update({
            'credits_remaining': credits - 1
        }).eq('id', user_id).execute()
        
        logger.info(f"Succès - ID: {insert_response.data[0]['id']}")
        
        return jsonify({
            'success': True,
            'summary_id': insert_response.data[0]['id'],
            'summary': summary,
            'metadata': transcript_data['metadata'],
            'credits_remaining': credits - 1
        }), 200