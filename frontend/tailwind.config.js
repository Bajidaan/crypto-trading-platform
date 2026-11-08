/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        darkbg: '#0a0d14',
        darkpanel: '#111622',
        darkcard: '#161c2b',
        darkborder: '#232c40',
        brandgreen: '#00c087',
        brandred: '#f6465d',
      }
    },
  },
  plugins: [],
}
