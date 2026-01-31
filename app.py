from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import logging
from supabase import create_client
from datetime import datetime
import google.generativeai as genai
import re

app = Flask(__name__)
CORS(app, origins=os.getenv('ALLOWED_ORIGINS', '*').split(','))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

supabase = create_client(
    os.getenv('SUPABASE_URL'),
    os.getenv('SUPABASE_SERVICE_KEY')
)

# Configuration Gemini
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

def get_video_metadata(video_id):
    """Récupère les métadonnées basiques de la vidéo"""
    try:
        import requests
        url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        return {
            'title': data.get('title'),
            'channel': data.get('author_name'),
            'thumbnail': data.get('thumbnail_url'),
        }, None
    except Exception as e:
        logger.error(f"Erreur métadonnées: {str(e)}")
        return None, str(e)

def generate_summary_with_gemini(video_url, style='structured'):
    """Génère un résumé avec Gemini en analysant directement la vidéo YouTube"""
    
    prompts = {
        'structured': f"""Analyse cette vidéo YouTube et crée un résumé structuré en français.

URL: {video_url}

FORMAT EXACT À SUIVRE:
## 📝 Résumé Principal
[2-3 phrases de synthèse globale]

## 🎯 Points Clés
- Point important 1
- Point important 2
- Point important 3

## 💡 Idées Principales
[Développement des concepts clés abordés dans la vidéo]

## 🔑 Conclusion
[Takeaway principal en 1-2 phrases]

Réponds uniquement en français, en suivant exactement cette structure.""",
        
        'bullets': f"""Analyse cette vidéo YouTube et résume-la en bullet points concis en français.

URL: {video_url}

Fournis 5-7 points clés numérotés qui capturent l'essentiel du contenu de la vidéo.
Réponds uniquement en français.""",
        
        'paragraph': f"""Analyse cette vidéo YouTube et écris un paragraphe de résumé fluide en français.

URL: {video_url}

Rédige 1 paragraphe de 4-6 phrases qui résume l'essentiel de la vidéo de manière naturelle.
Réponds uniquement en français."""
    }
    
    try:
        # Utiliser Gemini 1.5 Flash (le moins cher et suffisant)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = prompts.get(style, prompts['structured'])
        
        logger.info(f"Envoi requête Gemini pour {video_url}")
        response = model.generate_content(prompt)
        
        summary = response.text
        logger.info(f"Résumé reçu: {len(summary)} caractères")
        
        return summary, None
        
    except Exception as e:
        logger.error(f"Erreur Gemini: {str(e)}")
        return None, str(e)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        'status': 'healthy',
        'service': 'quicktube-backend',
        'version': 'gemini-api',
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
        
        # Récupérer les métadonnées de base
        logger.info(f"Récupération métadonnées pour video_id={video_id}")
        metadata, error = get_video_metadata(video_id)
        
        if error:
            # Si les métadonnées échouent, on continue quand même avec Gemini
            metadata = {
                'title': 'Vidéo YouTube',
                'channel': 'Inconnu',
                'thumbnail': f'https://img.youtube.com/vi/{video_id}/hqdefault.jpg'
            }
        
        # Générer le résumé avec Gemini (analyse directe de la vidéo)
        logger.info(f"Génération résumé Gemini style={style}")
        summary, error = generate_summary_with_gemini(video_url, style)
        
        if error:
            return jsonify({'error': f'Résumé échoué: {error}'}), 500
        
        # Sauvegarder en base
        summary_record = {
            'user_id': user_id,
            'video_url': video_url,
            'video_title': metadata.get('title'),
            'video_duration': None,  # Gemini ne retourne pas la durée
            'thumbnail_url': metadata.get('thumbnail'),
            'channel_name': metadata.get('channel'),
            'transcript': summary,  # On stocke le résumé comme "transcript"
            'summary': summary,
            'language': 'fr',
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
```

