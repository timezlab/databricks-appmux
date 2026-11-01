// Tailwind v4 is the only PostCSS plugin; it also does the vendor prefixing and
// the nesting, so nothing else belongs here.
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
