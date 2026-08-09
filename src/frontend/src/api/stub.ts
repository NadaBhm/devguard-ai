export const STUB = true

export async function mockList<T>(data: T[], delay = 400): Promise<T[]> {
  await new Promise((r) => setTimeout(r, delay))
  return data
}