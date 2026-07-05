#!/usr/bin/env node
/**
 * Build script for MediaMate Frontend
 *
 * This script injects environment variables into the HTML for production deployment.
 * It reads the API_BASE_URL environment variable and replaces the placeholder
 * in index.html with the actual backend URL.
 */

const fs = require("fs");
const path = require("path");

const INDEX_HTML = path.join(__dirname, "index.html");
const DIST_DIR = path.join(__dirname, "dist");

// Create dist directory if it doesn't exist
if (!fs.existsSync(DIST_DIR)) {
  fs.mkdirSync(DIST_DIR, { recursive: true });
}

// Read the index.html file
let html = fs.readFileSync(INDEX_HTML, "utf-8");

// Get API base URL from environment
// Priority: REACT_APP_API_BASE > API_BASE_URL > default to /api
const apiBaseUrl = process.env.REACT_APP_API_BASE ||
                   process.env.API_BASE_URL ||
                   "";

console.log(`Building frontend with API_BASE_URL: ${apiBaseUrl}`);

// Replace the API_BASE placeholder with the actual URL
html = html.replace(/const API_BASE = "{{API_BASE_URL}}";/, `const API_BASE = "${apiBaseUrl}";`);

// Write to dist folder
const outputPath = path.join(DIST_DIR, "index.html");
fs.writeFileSync(outputPath, html);

console.log(`✓ Frontend built successfully to ${outputPath}`);

