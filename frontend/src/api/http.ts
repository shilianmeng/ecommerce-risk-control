import axios from 'axios'

declare module 'axios' {
  interface AxiosRequestConfig { silent?: boolean; timeout?: number }
}
import { ElMessage } from 'element-plus'

const http = axios.create({ baseURL: '/api/v1', timeout: 30000 })

http.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('risk_token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

http.interceptors.response.use(
  (r) => r.data,
  (err) => {
    const status = err.response?.status
    let detail = err.response?.data?.detail
    if (Array.isArray(detail)) detail = detail.map((d: any) => d.msg).join('；')
    if (status === 401) {
      localStorage.removeItem('risk_token')
      if (location.pathname !== '/login') location.href = '/login'
    }
    if (!err.config?.silent) ElMessage.error(detail || (status ? `请求失败（${status}）` : '后端连接失败，请确认后端已启动（8092）'))
    return Promise.reject(err)
  },
)

// 返回值已经在拦截器里解包成 data
export const get = <T = any>(url: string, params?: any) => http.get(url, { params }) as Promise<T>
export const post = <T = any>(url: string, data?: any, cfg?: { silent?: boolean; timeout?: number }) => http.post(url, data, cfg as any) as Promise<T>
export const put = <T = any>(url: string, data?: any) => http.put(url, data) as Promise<T>
export const del = <T = any>(url: string) => http.delete(url) as Promise<T>