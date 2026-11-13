#version 330
// SAMPLING
out vec4 result;
void main() { result = texelFetch(iChannel0, ivec2(gl_FragCoord.xy), 0); }
