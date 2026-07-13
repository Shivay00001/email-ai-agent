'use client';

import { useState, useEffect } from 'react';

type Draft = {
  id: number;
  sender_email: string;
  subject: string;
  content: string;
  timestamp: string;
};

export default function Home() {
  const [activeTab, setActiveTab] = useState<'settings' | 'drafts'>('drafts');
  const [prompt, setPrompt] = useState('');
  const [keys, setKeys] = useState({ openai: '', anthropic: '', gemini: '', glm: '' });
  const [provider, setProvider] = useState('gpt-4o');
  const [sendgridKey, setSendgridKey] = useState('');
  const [status, setStatus] = useState<'idle' | 'saving' | 'success' | 'error'>('idle');
  const [message, setMessage] = useState('');
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [selectedDraft, setSelectedDraft] = useState<Draft | null>(null);
  const [editedContent, setEditedContent] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      setKeys({
        openai: localStorage.getItem('email_openai_key') || '',
        anthropic: localStorage.getItem('email_anthropic_key') || '',
        gemini: localStorage.getItem('email_gemini_key') || '',
        glm: localStorage.getItem('email_glm_key') || ''
      });
      setProvider(localStorage.getItem('email_llm_provider') || 'gpt-4o');
      setSendgridKey(localStorage.getItem('email_sendgrid_key') || '');

      try {
        const resPrompt = await fetch('http://localhost:8004/api/settings/prompt');
        if (resPrompt.ok) {
          const data = await resPrompt.json();
          setPrompt(data.system_prompt || '');
        }
        
        const resDrafts = await fetch('http://localhost:8004/api/emails/drafts');
        if (resDrafts.ok) {
          const draftsData = await resDrafts.json();
          setDrafts(draftsData);
        }
      } catch (e) {
        console.error("Failed to fetch data", e);
      } finally {
        setLoading(false);
      }
    };
    fetchData();
  }, [activeTab]);

  const handleSavePrompt = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatus('saving');
    
    try {
      localStorage.setItem('email_openai_key', keys.openai);
      localStorage.setItem('email_anthropic_key', keys.anthropic);
      localStorage.setItem('email_gemini_key', keys.gemini);
      localStorage.setItem('email_glm_key', keys.glm);
      localStorage.setItem('email_llm_provider', provider);
      localStorage.setItem('email_sendgrid_key', sendgridKey);

      await fetch('http://localhost:8004/api/settings/keys', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          openai_api_key: keys.openai, 
          anthropic_api_key: keys.anthropic,
          gemini_api_key: keys.gemini,
          glm_api_key: keys.glm,
          llm_provider: provider,
          sendgrid_api_key: sendgridKey 
        }),
      });

      const res = await fetch('http://localhost:8004/api/settings/prompt', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ system_prompt: prompt }),
      });
      
      if (res.ok) {
        setStatus('success');
        setMessage('Settings updated!');
        setTimeout(() => setStatus('idle'), 3000);
      } else {
        setStatus('error');
        setMessage('Failed to save settings.');
      }
    } catch (e) {
      setStatus('error');
      setMessage('Network error.');
    }
  };

  const handleApproveDraft = async () => {
    if (!selectedDraft) return;
    setStatus('saving');
    
    try {
      const res = await fetch(`http://localhost:8004/api/emails/drafts/${selectedDraft.id}/approve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ edited_content: editedContent }),
      });
      
      if (res.ok) {
        setStatus('success');
        setMessage('Email approved and sent!');
        setDrafts(drafts.filter(d => d.id !== selectedDraft.id));
        setSelectedDraft(null);
        setTimeout(() => setStatus('idle'), 3000);
      } else {
        setStatus('error');
        setMessage('Failed to send email.');
      }
    } catch (e) {
      setStatus('error');
      setMessage('Network error.');
    }
  };

  return (
    <main className="dashboard-container" style={{ maxWidth: '1000px' }}>
      <div className="dashboard-header">
        <h1>Email AI Agent</h1>
        <p>Human-in-the-Loop Review Dashboard</p>
      </div>

      <div style={{ display: 'flex', gap: '20px', marginBottom: '30px', justifyContent: 'center' }}>
        <button 
          onClick={() => setActiveTab('drafts')}
          className="save-btn"
          style={{ opacity: activeTab === 'drafts' ? 1 : 0.5, padding: '10px 20px' }}
        >
          Drafts Queue ({drafts.length})
        </button>
        <button 
          onClick={() => setActiveTab('settings')}
          className="save-btn"
          style={{ opacity: activeTab === 'settings' ? 1 : 0.5, padding: '10px 20px' }}
        >
          Agent Settings
        </button>
      </div>

      {loading ? (
        <p>Loading...</p>
      ) : activeTab === 'settings' ? (
        <form onSubmit={handleSavePrompt}>
          <div className="form-group">
            <label htmlFor="prompt">System Prompt (Instructions)</label>
            <textarea 
              id="prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              required
            />
          </div>
          <div className="form-group" style={{marginTop:'20px'}}>
            <label>OpenAI Key</label>
            <input type="password" value={keys.openai} onChange={(e)=>setKeys({...keys, openai: e.target.value})} />
          </div>
          <div className="form-group" style={{marginTop:'20px'}}>
            <label>Anthropic Key</label>
            <input type="password" value={keys.anthropic} onChange={(e)=>setKeys({...keys, anthropic: e.target.value})} />
          </div>
          <div className="form-group" style={{marginTop:'20px'}}>
            <label>Gemini Key</label>
            <input type="password" value={keys.gemini} onChange={(e)=>setKeys({...keys, gemini: e.target.value})} />
          </div>
          <div className="form-group" style={{marginTop:'20px'}}>
            <label>ZhipuAI Key</label>
            <input type="password" value={keys.glm} onChange={(e)=>setKeys({...keys, glm: e.target.value})} />
          </div>
          <div className="form-group" style={{marginTop:'20px'}}>
            <label>LLM Engine</label>
            <select value={provider} onChange={(e)=>setProvider(e.target.value)} style={{width: '100%', padding: '10px'}}>
              <option value="gpt-4o">OpenAI (gpt-4o)</option>
              <option value="claude-3-5-sonnet-20240620">Anthropic (claude-3-5-sonnet)</option>
              <option value="gemini/gemini-1.5-pro">Google AI (gemini-1.5-pro)</option>
              <option value="zhipu/glm-4">ZhipuAI (glm-4)</option>
            </select>
          </div>
          <div className="form-group" style={{marginTop:'20px'}}>
            <label>SendGrid API Key</label>
            <input type="password" value={sendgridKey} onChange={(e)=>setSendgridKey(e.target.value)} placeholder="SG...." />
          </div>
          <button type="submit" className="save-btn" disabled={status === 'saving'} style={{marginTop:'20px'}}>
            {status === 'saving' ? 'Saving...' : 'Update Agent'}
          </button>
        </form>
      ) : (
        <div style={{ display: 'flex', gap: '20px', textAlign: 'left' }}>
          <div style={{ flex: 1, borderRight: '1px solid var(--glass-border)', paddingRight: '20px' }}>
            <h3 style={{ marginBottom: '15px', color: 'var(--text-secondary)' }}>Pending Drafts</h3>
            {drafts.length === 0 ? <p>No pending emails.</p> : drafts.map(draft => (
              <div 
                key={draft.id} 
                onClick={() => { setSelectedDraft(draft); setEditedContent(draft.content); }}
                style={{ 
                  padding: '15px', 
                  background: selectedDraft?.id === draft.id ? 'rgba(88, 166, 255, 0.2)' : 'rgba(0,0,0,0.2)',
                  borderRadius: '8px',
                  marginBottom: '10px',
                  cursor: 'pointer',
                  border: '1px solid var(--glass-border)'
                }}
              >
                <div style={{ fontWeight: 'bold' }}>To: {draft.sender_email}</div>
                <div style={{ fontSize: '0.9rem', color: '#ccc' }}>Subject: {draft.subject}</div>
              </div>
            ))}
          </div>
          
          <div style={{ flex: 2, paddingLeft: '10px' }}>
            {selectedDraft ? (
              <div className="form-group">
                <label>Review & Edit Draft</label>
                <textarea 
                  value={editedContent}
                  onChange={(e) => setEditedContent(e.target.value)}
                  style={{ minHeight: '300px' }}
                />
                <div style={{ marginTop: '20px' }}>
                  <button onClick={handleApproveDraft} className="save-btn" disabled={status === 'saving'}>
                    Approve & Send
                  </button>
                </div>
              </div>
            ) : (
              <p>Select a draft to review.</p>
            )}
          </div>
        </div>
      )}

      {status !== 'idle' && status !== 'saving' && (
        <div className={`status-message ${status}`} style={{ marginTop: '20px' }}>
          {message}
        </div>
      )}
    </main>
  );
}
