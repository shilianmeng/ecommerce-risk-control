import { defineStore } from 'pinia'
import { post } from '@/api/http'

export interface User { username: string; name: string; role: string; role_cn: string }

export const useAuth = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem('risk_token') || '',
    user: JSON.parse(localStorage.getItem('risk_user') || 'null') as User | null,
  }),
  getters: {
    can: (s) => (...roles: string[]) => !!s.user && (s.user.role === 'admin' || roles.includes(s.user.role)),
  },
  actions: {
    async login(username: string, password: string) {
      const r = await post<{ token: string; user: User }>('/auth/login', { username, password })
      this.token = r.token
      this.user = r.user
      localStorage.setItem('risk_token', r.token)
      localStorage.setItem('risk_user', JSON.stringify(r.user))
    },
    logout() {
      this.token = ''
      this.user = null
      localStorage.removeItem('risk_token')
      localStorage.removeItem('risk_user')
    },
  },
})