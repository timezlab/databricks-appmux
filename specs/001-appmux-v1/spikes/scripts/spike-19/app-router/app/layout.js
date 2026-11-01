import "./globals.css";

export const metadata = { title: "appmux spike 19" };

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
