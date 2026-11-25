export const EVENT_TYPES: Record<string, string> = {
  login: '登录', coupon_receive: '领券', order_create: '下单', order_pay: '支付', after_sale_apply: '售后申请',
}
export const DECISION_TAG: Record<string, { text: string; type: 'success' | 'warning' | 'danger' }> = {
  PASS: { text: '通过', type: 'success' }, REVIEW: { text: '人审', type: 'warning' }, REJECT: { text: '拒绝', type: 'danger' },
}
export const CASE_STATUS: Record<string, { text: string; type: 'info' | 'warning' | 'success' | 'primary' }> = {
  PENDING: { text: '待审', type: 'warning' }, IN_REVIEW: { text: '审核中', type: 'primary' },
  DISPOSED: { text: '已处置', type: 'success' }, ARCHIVED: { text: '已归档', type: 'info' },
}
export const REFUND_REASONS: Record<string, string> = { not_received: '未收到货', quality: '质量问题', no_reason: '不想要了', other: '其他' }
export const LIST_TYPES: Record<string, string> = { white: '白名单', black: '黑名单', grey: '灰名单' }
export const ENTITY_TYPES: Record<string, string> = { user: '账号', device: '设备', ip: 'IP', phone: '手机号', address: '地址' }
export const ACTIONS: Record<string, string> = {
  block_order: '拦截订单', blacklist_user: '账号拉黑', ban_device: '设备封禁', release: '放行',
}
export const ROLE_CN: Record<string, string> = { admin: '管理员', strategist: '策略师', auditor: '审核员' }
export const fmtTime = (s?: string) => (s ? s.replace('T', ' ').slice(0, 19) : '')