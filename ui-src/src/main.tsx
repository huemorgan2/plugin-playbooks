import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { installAuthListener } from './lib/auth'
import { App } from './App'
import './index.css'

// Auth first: the listener must be live before the Shell's luna-auth post
// (which fires on the iframe load event, i.e. after this module runs).
installAuthListener()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
